import contextlib
import copy
import hashlib
import logging
import threading

from django.db import connection
from django.db import models
from django.db.models.expressions import Col
from django.db.models.fields.related import RelatedField
from django.db.models.sql import Query
from django.db.models.sql.datastructures import BaseTable
import pgconnection


LOGGER = logging.getLogger('pgtrigger')
_unset = object()

# All registered triggers for each model
registry = {}

# All triggers currently being ignored
_ignore = threading.local()


def _is_concurrent_statement(sql):
    """
    True if the sql statement is concurrent and cannot be ran in a transaction
    """
    sql = sql.strip().lower() if sql else ''
    return sql.startswith('create') and 'concurrently' in sql


def _inject_pgtrigger_ignore(sql, sql_vars, cursor):  # pragma: no cover
    """
    A pgconnection pre_execute hook that sets a pgtrigger.ignore
    variable in the executed SQL. This lets other triggers know when
    they should ignore execution
    """
    if cursor.name:
        # A named cursor automatically prepends
        # "NO SCROLL CURSOR WITHOUT HOLD FOR" to the query, which
        # causes invalid SQL to be generated. There is no way
        # to override this behavior in psycopg2, so context tracking
        # is ignored for named cursors. Django only names cursors
        # for iterators and other statements that read the database,
        # so it seems to be safe to ignore named cursors.
        # TODO(@wesleykendall): Find a way to generate valid SQL
        # for local variables within a named cursor declaration.
        return None
    elif _is_concurrent_statement(sql):
        # Concurrent index creation is incompatible with local variable
        # setting. Ignore this specific statement for now
        return None

    sql = (
        'SET LOCAL pgtrigger.ignore=\'{' + ','.join(_ignore.value) + '}\';'
    ) + sql

    return sql, sql_vars


def register(*triggers):
    """
    Register the given triggers with wrapped Model class
    """

    def _model_wrapper(model_class):
        for trigger in triggers:
            trigger.register(model_class)

        return model_class

    return _model_wrapper


class _Level:
    def __init__(self, name):
        self.name = name

    def __str__(self):
        return self.name


#: For specifying row-level triggers (the default)
Row = _Level('ROW')

#: For specifying statement-level triggers
Statement = _Level('STATEMENT')


class Referencing:
    """For specifying the REFERENCING construct of a statement-level trigger"""

    def __init__(self, *, old=None, new=None):
        if not old and not new:
            raise ValueError(
                'Must provide either "old" and/or "new" to the referencing'
                ' construct of a trigger'
            )

        self.old = old
        self.new = new

    def __str__(self):
        ref = 'REFERENCING'
        if self.old:
            ref += f' OLD TABLE AS {self.old} '

        if self.new:
            ref += f' NEW TABLE AS {self.new} '

        return ref


class _When:
    def __init__(self, name):
        self.name = name

    def __str__(self):
        return self.name


#: For specifying "BEFORE" in the "when" clause of a trigger
Before = _When('BEFORE')

#: For specifying "AFTER" in the "when" clause of a trigger
After = _When('AFTER')

#: For specifying "INSTEAD OF" in the "when" clause of a trigger
InsteadOf = _When('INSTEAD OF')


class _Operation:
    def __init__(self, name):
        self.name = name

    def __or__(self, other):
        return _Operations(self, other)

    def __str__(self):
        return self.name


class _Operations(_Operation):
    def __init__(self, *operations):
        self.operations = operations

    def __str__(self):
        return ' OR '.join(str(operation) for operation in self.operations)


#: For specifying "UPDATE" in the "operation" clause of a trigger
Update = _Operation('UPDATE')

#: For specifying "DELETE" in the "operation" clause of a trigger
Delete = _Operation('DELETE')

#: For specifying "TRUNCATE" in the "operation" clause of a trigger
Truncate = _Operation('TRUNCATE')

#: For specifying "INSERT" in the "operation" clause of a trigger
Insert = _Operation('INSERT')


class UpdateOf(_Operation):
    """For specifying "UPDATE OF" in the "operation" clause of a trigger"""

    def __init__(self, *columns):
        if not columns:
            raise ValueError('Must provide at least one column')

        self.columns = ', '.join(f'"{col}"' for col in columns)

    def __str__(self):
        return f'UPDATE OF {self.columns}'


class Condition:
    """For specifying free-form SQL in the "condition" clause of a trigger"""

    sql = None

    def __init__(self, sql=None):
        self.sql = sql or self.sql

        if not self.sql:
            raise ValueError('Must provide SQL to condition')

    def __str__(self):
        return self.sql

    def resolve(self, model):
        return self.sql


class _OldNewQuery(Query):
    """
    A special Query object for referencing the OLD and NEW variables in a
    trigger. Only used by the Q object
    """

    def build_lookup(self, lookups, lhs, rhs):
        # Django does not allow custom lookups on foreign keys, even though
        # DISTINCT FROM is a comnpletely valid lookup. Trick django into
        # being able to apply this lookup to related fields.
        if lookups == ['df'] and isinstance(lhs.output_field, RelatedField):
            lhs = copy.deepcopy(lhs)
            lhs.output_field = models.IntegerField(null=lhs.output_field.null)

        return super().build_lookup(lookups, lhs, rhs)

    def build_filter(self, filter_expr, *args, **kwargs):
        if filter_expr[0].startswith('old__'):
            alias = 'OLD'
        elif filter_expr[0].startswith('new__'):
            alias = 'NEW'
        else:  # pragma: no cover
            raise ValueError(
                'Filter expression on trigger.Q object must reference'
                ' old__ or new__'
            )

        filter_expr = (filter_expr[0][5:], filter_expr[1])
        node, _ = super().build_filter(filter_expr, *args, **kwargs)

        self.alias_map[alias] = BaseTable(alias, alias)
        for child in node.children:
            child.lhs = Col(
                alias=alias,
                target=child.lhs.target,
                output_field=child.lhs.output_field,
            )

        return node, {alias}


class F(models.F):
    """
    Similar to Django's F object, allows referencing the old and new
    rows in a trigger condition.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        if self.name.startswith('old__'):
            self.row_alias = 'OLD'
        elif self.name.startswith('new__'):
            self.row_alias = 'NEW'
        else:
            raise ValueError('F() values must reference old__ or new__')

        self.col_name = self.name[5:]

    @property
    def resolved_name(self):
        return f'{self.row_alias}."{self.col_name}"'

    def resolve_expression(self, query=None, *args, **kwargs):
        return Col(
            alias=self.row_alias,
            target=query.model._meta.get_field(self.col_name),
        )


@models.fields.Field.register_lookup
class IsDistinctFrom(models.Lookup):
    """
    A custom IS DISTINCT FROM field lookup for common trigger conditions
    """

    lookup_name = 'df'

    def as_sql(self, compiler, connection):
        lhs, lhs_params = self.process_lhs(compiler, connection)
        rhs, rhs_params = self.process_rhs(compiler, connection)
        params = lhs_params + rhs_params
        return '%s IS DISTINCT FROM %s' % (lhs, rhs), params


@models.fields.Field.register_lookup
class IsNotDistinctFrom(models.Lookup):
    """
    A custom IS NOT DISTINCT FROM field lookup for common trigger conditions
    """

    lookup_name = 'ndf'

    def as_sql(self, compiler, connection):
        lhs, lhs_params = self.process_lhs(compiler, connection)
        rhs, rhs_params = self.process_rhs(compiler, connection)
        params = lhs_params + rhs_params
        return '%s IS NOT DISTINCT FROM %s' % (lhs, rhs), params


class Q(models.Q, Condition):
    """
    Similar to Django's Q object, allows referencing the old and new
    rows in a trigger condition.
    """

    def resolve(self, model):
        query = _OldNewQuery(model)
        sql = (
            connection.cursor()
            .mogrify(
                *self.resolve_expression(query).as_sql(
                    compiler=query.get_compiler('default'),
                    connection=connection,
                )
            )
            .decode()
            .replace('"OLD"', 'OLD')
            .replace('"NEW"', 'NEW')
        )

        return sql


def _drop_trigger(table, trigger_pgid):
    with connection.cursor() as cursor:
        cursor.execute(f'DROP TRIGGER IF EXISTS {trigger_pgid} ON {table};')


# Allows Trigger methods to be used as context managers, mostly for
# testing purposes
@contextlib.contextmanager
def _cleanup_on_exit(cleanup):
    yield
    cleanup()


class Trigger:
    """
    For specifying a free-form PL/pgSQL trigger function or for
    creating derived trigger classes.
    """

    name = None
    level = Row
    when = None
    operation = None
    condition = None
    referencing = None
    func = None

    def __init__(
        self,
        *,
        name=None,
        level=None,
        when=None,
        operation=None,
        condition=None,
        referencing=None,
        func=None,
    ):
        self._name = name
        self.level = level or self.level
        self.when = when or self.when
        self.operation = operation or self.operation
        self.condition = condition or self.condition
        self.referencing = referencing or self.referencing
        self.func = func or self.func

        if not self.level or not isinstance(self.level, _Level):
            raise ValueError(f'Invalid "level" attribute: {self.level}')

        if not self.when or not isinstance(self.when, _When):
            raise ValueError(f'Invalid "when" attribute: {self.when}')

        if not self.operation or not isinstance(self.operation, _Operation):
            raise ValueError(
                f'Invalid "operation" attribute: {self.operation}'
            )

        if self.level == Row and self.referencing:
            raise ValueError(
                'Row-level triggers cannot have a "referencing" attribute'
            )

    def __str__(self):
        return self.name

    def __hash__(self):
        return hash(self.name)

    def get_key(self):
        """The unique key for the trigger when generating an alias"""
        return list(self.__dict__.values())

    @property
    def name(self):
        if not self._name:
            hash = hashlib.sha1(
                ''.join(str(k) for k in self.get_key()).encode()
            ).hexdigest()[:16]
            return f'{self.__class__.__name__.lower()}_'[:37] + str(hash)
        else:
            return self._name

    @property
    def pgid(self):
        """The ID of the trigger and function object in postgres

        All objects are prefixed with "pgtrigger_" in order to be
        discovered/managed by django-pgtrigger
        """
        if len(self.name) > 53:
            raise ValueError(f'Trigger name "{self.name}" > 53 characters. ')

        return f'pgtrigger_{self.name}'

    def get_condition(self, model):
        return self.condition

    def get_declare(self, model):
        """
        Gets the DECLARE part of the trigger function if any variables
        are used.

        Returns:
            List[tuple]: A list of variable name / type tuples that will
            be shown in the DECLARE. For example [('row_data', 'JSONB')]
        """
        return []

    def get_func(self, model):
        """
        Returns the trigger function that comes between the BEGIN and END
        clause
        """
        if not self.func:
            raise ValueError(
                'Must define func attribute or implement get_func'
            )
        return self.func

    def get_uri(self, model):
        """The URI for the trigger in the registry"""
        return (
            f'{model._meta.app_label}.{model._meta.object_name}'
            f':{self.name}'
        )

    def register(self, *models):
        """Register model classes with the trigger"""
        for model in models:
            registry[self.get_uri(model)] = (model, self)

        return _cleanup_on_exit(lambda: self.unregister(*models))

    def unregister(self, *models):
        """Unregister model classes with the trigger"""
        for model in models:
            del registry[self.get_uri(model)]

        return _cleanup_on_exit(lambda: self.register(*models))

    def render_condition(self, model):
        """Renders the condition SQL in the trigger declaration"""
        condition = self.get_condition(model)
        resolved = condition.resolve(model).strip() if condition else ''

        if resolved:
            if not resolved.startswith('('):
                resolved = f'({resolved})'
            resolved = f'WHEN {resolved}'

        return resolved

    def render_declare(self, model):
        """Renders the DECLARE of the trigger function, if any"""
        declare = self.get_declare(model)
        if declare:
            rendered_declare = 'DECLARE \n' + '\n'.join(
                f'{var_name} {var_type};' for var_name, var_type in declare
            )
        else:
            rendered_declare = ''

        return rendered_declare

    def render_ignore(self, model):
        """
        Renders the clause that can dynamically ignore the trigger's execution
        """
        return '''
            IF (_pgtrigger_should_ignore(TG_TABLE_NAME, TG_NAME) IS TRUE) THEN
                IF (TG_OP = 'DELETE') THEN
                    RETURN OLD;
                ELSE
                    RETURN NEW;
                END IF;
            END IF;
        '''

    def render_func(self, model):
        """Renders the trigger function SQL statement"""
        return f'''
            CREATE OR REPLACE FUNCTION {self.pgid}()
            RETURNS TRIGGER AS $$
                {self.render_declare(model)}
                BEGIN
                    {self.render_ignore(model)}
                    {self.get_func(model)}
                END;
            $$ LANGUAGE plpgsql;
        '''

    def render_trigger(self, model):
        """Renders the trigger declaration SQL statement"""
        table = model._meta.db_table
        return f'''
            DO $$ BEGIN
                CREATE TRIGGER {self.pgid}
                    {self.when} {self.operation} ON {table}
                    {self.referencing or ''}
                    FOR EACH {self.level} {self.render_condition(model)}
                    EXECUTE PROCEDURE {self.pgid}();
            EXCEPTION
                -- Ignore issues if the trigger already exists
                WHEN others THEN null;
            END $$;
        '''

    def install(self, model):
        """Installs the trigger for a model"""

        # Ensure we have the function to ignore execution of triggers
        install_ignore_func()

        rendered_func = self.render_func(model)
        rendered_trigger = self.render_trigger(model)

        with connection.cursor() as cursor:
            cursor.execute(rendered_func)
            cursor.execute(rendered_trigger)

        return _cleanup_on_exit(lambda: self.uninstall(model))

    def uninstall(self, model):
        """Uninstalls the trigger for a model"""
        _drop_trigger(model._meta.db_table, self.pgid)

        return _cleanup_on_exit(  # pragma: no branch
            lambda: self.install(model)
        )

    def enable(self, model):
        """Enables the trigger for a model"""
        with connection.cursor() as cursor:
            cursor.execute(
                f'ALTER TABLE {model._meta.db_table} ENABLE TRIGGER {self.pgid};'
            )

        return _cleanup_on_exit(  # pragma: no branch
            lambda: self.disable(model)
        )

    def disable(self, model):
        """Disables the trigger for a model"""
        with connection.cursor() as cursor:
            cursor.execute(
                f'ALTER TABLE {model._meta.db_table} DISABLE TRIGGER {self.pgid};'
            )

        return _cleanup_on_exit(  # pragma: no branch
            lambda: self.enable(model)
        )

    @contextlib.contextmanager
    def ignore(self, model):
        """Ignores the trigger in a single thread of execution"""
        with contextlib.ExitStack() as pre_execute_hook:

            # Create the table name / trigger name URI to pass down to the
            # trigger.
            ignore_uri = f'{model._meta.db_table}:{self.pgid}'

            if not hasattr(_ignore, 'value'):
                _ignore.value = set()

            if not _ignore.value:
                # If this is the first time we are ignoring trigger execution,
                # register the pre_execute_hook
                pre_execute_hook.enter_context(
                    pgconnection.pre_execute_hook(_inject_pgtrigger_ignore)
                )

            if ignore_uri not in _ignore.value:
                try:
                    _ignore.value.add(ignore_uri)
                    yield
                finally:
                    _ignore.value.remove(ignore_uri)
            else:  # The trigger is already being ignored
                yield


class Protect(Trigger):
    """A trigger that raises an exception"""

    when = Before

    def get_func(self, model):
        return f'''
            RAISE EXCEPTION
                'pgtrigger: Cannot {str(self.operation).lower()} rows from % table',
                TG_TABLE_NAME;
        '''


class FSM(Trigger):
    """Enforces a finite state machine on a field.

    Supply the trigger with the "field" that transitions and then
    a list of tuples of valid transitions to the "transitions" argument.

    .. note::

        Only non-null ``CharField`` fields are currently supported.
    """

    when = Before
    operation = Update
    field = None
    transitions = None

    def __init__(self, *, condition=None, field=None, transitions=None):
        self.field = field or self.field
        self.transitions = transitions or self.transitions

        if not self.field:  # pragma: no cover
            raise ValueError('Must provide "field" for FSM')

        if not self.transitions:  # pragma: no cover
            raise ValueError('Must provide "transitions" for FSM')

        super().__init__(condition=condition)

    def get_declare(self, model):
        return [('_is_valid_transition', 'BOOLEAN')]

    def get_func(self, model):
        col = model._meta.get_field(self.field).column
        transition_uris = (
            '{'
            + ','.join([f'{old}:{new}' for old, new in self.transitions])
            + '}'
        )

        return f'''
            SELECT CONCAT(OLD.{col}, ':', NEW.{col}) = ANY('{transition_uris}'::text[])
                INTO _is_valid_transition;

            IF (_is_valid_transition IS FALSE AND OLD.{col} IS DISTINCT FROM NEW.{col}) THEN
                RAISE EXCEPTION
                    'pgtrigger: Invalid transition of field "{self.field}" from "%" to "%" on table %',
                    OLD.{col},
                    NEW.{col},
                    TG_TABLE_NAME;
            ELSE
                RETURN NEW;
            END IF;
        '''  # noqa


class SoftDelete(Trigger):
    """Sets a field to a value when a delete happens.

    Supply the trigger with the "field" that will be set
    upon deletion and the "value" to which it should be set.
    The "value" defaults to False.

    .. note::

        This trigger currently only supports nullable ``BooleanField``,
        ``CharField``, and ``IntField`` fields.
    """

    when = Before
    operation = Delete
    field = None
    value = False

    def __init__(self, *, name=None, condition=None, field=None, value=_unset):
        self.field = field or self.field
        self.value = value if value is not _unset else self.value

        if not self.field:  # pragma: no cover
            raise ValueError('Must provide "field" for soft delete')

        super().__init__(name=name, condition=condition)

    def get_func(self, model):
        soft_field = model._meta.get_field(self.field).column
        pk_col = model._meta.pk.column

        def _render_value():
            if self.value is None:
                return 'NULL'
            elif isinstance(self.value, str):
                return f"'{self.value}'"
            else:
                return str(self.value)

        return f'''
            UPDATE {model._meta.db_table}
            SET {soft_field} = {_render_value()}
            WHERE "{pk_col}" = OLD."{pk_col}";
            RETURN NULL;
        '''


def get(*uris):
    """
    Get triggers matching URIs or all triggers registered to models

    A URI is in the format of "app_label.model_name:trigger_name"
    """
    if uris:
        for uri in uris:
            if uri and len(uri.split(':')) == 1:
                raise ValueError(
                    'Trigger URI must be in the format of'
                    ' "app_label.model_name:trigger_name"'
                )
            elif uri and uri not in registry:
                raise ValueError(
                    f'URI "{uri}" not found in pgtrigger registry'
                )

        return [registry[uri] for uri in uris]
    else:
        return list(registry.values())


def install(*uris):
    """
    Install registered triggers matching URIs or all triggers if URIs aren't
    provided. If URIs aren't provided, prune any orphaned triggers from the
    database
    """
    for model, trigger in get(*uris):
        LOGGER.info(
            f'pgtrigger: Installing "{trigger}" trigger for {model._meta.db_table} table.'
        )
        trigger.install(model)

    if not uris:  # pragma: no branch
        prune()


def prune():
    """
    Remove any pgtrigger triggers in the database that are not used by models.
    I.e. if a model or trigger definition is deleted from a model, ensure
    it is removed from the database
    """
    installed = {
        (model._meta.db_table, trigger.pgid) for model, trigger in get()
    }

    with connection.cursor() as cursor:
        cursor.execute(
            'SELECT event_object_table as table_name, trigger_name'
            '    FROM information_schema.triggers'
            '    WHERE trigger_name LIKE \'pgtrigger_%\''
        )
        triggers = set(cursor.fetchall())

    for trigger in triggers:
        if trigger not in installed:
            LOGGER.info(
                f'pgtrigger: Pruning trigger {trigger[1]}'
                f' from table {trigger[0]}...'
            )
            _drop_trigger(*trigger)


def enable(*uris):
    """
    Enables registered triggers matching URIs or all triggers if no URIs
    are provided
    """
    for model, trigger in get():
        LOGGER.info(
            f'pgtrigger: Enabling "{trigger}" trigger for {model._meta.db_table} table.'
        )
        trigger.enable(model)


def uninstall(*uris):
    """
    Uninstalls registered triggers matching URIs or all triggers if no
    URIs are provided.

    Running migrations will re-install any existing triggers. This
    behavior is overridable with ``settings.PGTRIGGER_INSTALL_ON_MIGRATE``

    Note: This will not uninstall triggers when deleting a model.
    This operation is performed by the "prune" command.
    """
    for model, trigger in get(*uris):
        LOGGER.info(
            f'pgtrigger: Uninstalling "{trigger}" trigger for {model._meta.db_table} table.'
        )
        trigger.uninstall(model)

    prune()


def disable(*uris):
    """
    Disables registered triggers matching URIs or all triggers if no URIs are
    provided
    """
    for model, trigger in get(*uris):
        LOGGER.info(
            f'pgtrigger: Disabling "{trigger}" trigger for {model._meta.db_table} table.'
        )
        trigger.disable(model)


@contextlib.contextmanager
def ignore(*uris):
    """
    Dynamically ignore registered triggers matching URIs from executing in
    an individual thread.
    If no URIs are provided, ignore all pgtriggers from executing in an
    individual thread.
    """
    with contextlib.ExitStack() as stack:
        for model, trigger in get(*uris):
            stack.enter_context(trigger.ignore(model))

        yield


def install_ignore_func():
    """
    pgtrigger uses a special postgres function to determine when a trigger
    should be ignored. This installs the function.

    This function is automatically installed when all triggers are installed
    with pgtrigger.install()
    """
    with connection.cursor() as cursor:
        cursor.execute(
            '''
            CREATE OR REPLACE FUNCTION _pgtrigger_should_ignore(
                table_name NAME,
                trigger_name NAME
            )
            RETURNS BOOLEAN AS $$
                DECLARE
                    _pgtrigger_ignore TEXT[];
                    _result BOOLEAN;
                BEGIN
                    BEGIN
                        SELECT INTO _pgtrigger_ignore
                            CURRENT_SETTING('pgtrigger.ignore');
                        EXCEPTION WHEN OTHERS THEN
                    END;
                    IF _pgtrigger_ignore IS NOT NULL THEN
                        SELECT CONCAT(table_name, ':', trigger_name) = ANY(_pgtrigger_ignore)
                        INTO _result;
                        RETURN _result;
                    ELSE
                        RETURN FALSE;
                    END IF;
                END;
            $$ LANGUAGE plpgsql;
            '''
        )
