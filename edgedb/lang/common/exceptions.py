##
# Copyright (c) 2010-2011 MagicStack Inc.
# All rights reserved.
#
# See LICENSE for details.
##


import collections
import sys
_old_excepthook = sys.excepthook


def _get_contexts(ex, *, auto_init=False):
    try:
        return ex.__sx_error_contexts__
    except AttributeError:
        if auto_init:
            cs = ex.__sx_error_contexts__ = collections.OrderedDict()
            return cs
        else:
            return {}


def _add_context(ex, context):
    assert isinstance(context, ExceptionContext)

    contexts = _get_contexts(ex, auto_init=True)

    cls = context.__class__
    if cls in contexts:
        raise ValueError('context {}.{} is already present in exception'. \
                         format(cls.__module__, cls.__name__))

    contexts[cls] = context


def _replace_context(ex, context):
    contexts = _get_contexts(ex, auto_init=True)
    contexts[context.__class__] = context


def _get_context(ex, context_class):
    contexts = _get_contexts(ex)
    try:
        return contexts[context_class]
    except KeyError as ex:
        raise LookupError('{} context class is not found'.format(context_class)) from ex


def _iter_contexts(ex, ctx_class=None):
    contexts = _get_contexts(ex)
    if ctx_class is None:
        return iter(contexts.values())
    else:
        assert issubclass(ctx_class, ExceptionContext)
        return (context for context in contexts.values() if isinstance(context, ctx_class))


def _sx_serialize(ex):
    cls = type(ex)
    body = {
        '$class': '{}.{}'.format(cls.__module__, cls.__name__),
        'args': [str(arg) for arg in ex.args]
    }
    return body


def sx_serialize(ex):
    try:
        serialize = ex.__mm_serialize__
    except AttributeError:
        return _sx_serialize(ex)
    else:
        return serialize()


class EdgeDBError(Exception):
    def __init__(self, msg=None, *, hint=None, details=None):
        super().__init__(msg)
        self.msg = msg
        self.details = details

        if (hint or details) is not None:
            _add_context(self, DefaultExceptionContext(hint=hint, details=details))

    def as_text(self):
        buffer = ''

        for context in _iter_contexts(self):
            buffer += context.as_text()

        return buffer

    def __mm_serialize__(self):
        return _sx_serialize(self)


class MultiError(EdgeDBError):
    def __init__(self, msg=None, *, errors):
        assert errors
        assert all(isinstance(error, BaseException) for error in errors)
        self.errors = errors

        if msg is None:
            msg = '\n'.join('{}: {}'.format(type(ex).__name__, ex) for ex in self.errors)

        super().__init__(msg)


class ExceptionContext:
    title = 'Exception Context'


class DefaultExceptionContext(ExceptionContext):
    title = 'Details'

    def __init__(self, hint=None, details=None):
        super().__init__()

        self.details = details
        self.hint = hint


class EdgeDBExceptionContext(ExceptionContext):
    pass


def excepthook(exctype, exc, tb):
    try:
        from edgedb.lang.common import markup
        markup.renderers.terminal.render(markup.serialize(exc, ctx=markup.Context()),
                                         file=sys.stderr)

    except Exception as ex:
        print('!!! exception in edgedb.excepthook !!!', file=sys.stderr)

        # Attach the original exception as a context to top of the new chain,
        # but only if it's not already there.  Take some care to avoid looping forever.
        visited = set()
        parent = ex
        while parent.__cause__ or (not parent.__suppress_context__ and parent.__context__):
            if parent in visited or parent.__context__ is exc or parent.__cause__ is exc:
                break
            visited.add(parent)
            parent = parent.__cause__ or parent.__context__
        parent.__context__ = exc
        parent.__cause__ = None

        _old_excepthook(type(ex), ex, ex.__traceback__)


def install_excepthook():
    """Installs edgedb excepthook, which renders exceptions contexts on
    EdgeDBErrors, and uses rich styled output if possible"""

    sys.excepthook = excepthook


def uninstall_excepthook():
    """Installs python's plain ``sys.excepthook`` back"""

    sys.excepthook = _old_excepthook


class CaosBackendError(EdgeDBError):
    pass


class ObjectError(EdgeDBError):
    def __init__(self, msg, *, details=None, object):
        super().__init__(msg, details=details)
        self.object = object


class SourceError(ObjectError):
    pass


class PointerErrorContext(EdgeDBExceptionContext):
    def __init__(self, source, pointer, pointer_proto):
        super().__init__()
        self.source, self.pointer = source, pointer

        if pointer_proto is not None:
            self.pointer_proto = pointer_proto
        elif pointer is not None:
            self.pointer_proto = pointer.__sx_prototype__
        else:
            self.pointer_proto = None

    @classmethod
    def as_markup(cls, self, *, ctx):
        me = markup.elements

        body = []
        body.append(me.doc.Text(text='SOURCE: {!r}'.format(self.source)))
        body.append(me.doc.Text(text='POINTER: {!r}'.
                                format(self.pointer or self.pointer_proto)))

        return me.lang.ExceptionContext(title=self.title, body=body)


class PointerError(ObjectError):
    def __init__(self, msg, *, details=None, pointer, pointer_proto=None,
                 source):
        super().__init__(msg, details=details, object=pointer)
        self.set_pointer_context(source, pointer, pointer_proto)

    def set_pointer_context(self, source, pointer, pointer_proto):
        ctx = PointerErrorContext(source, pointer, pointer_proto)
        _replace_context(self, ctx)
        self.source = source
        self.pointer = pointer
        self.pointer_proto = ctx.pointer_proto


class AtomError(EdgeDBError):
    pass


class CorrectnessError(EdgeDBError):
    pass


class AtomValueErrorContext(EdgeDBExceptionContext):
    def __init__(self, value):
        super().__init__()
        self.value = value
        self.title = 'Atom value details'

    @classmethod
    def as_markup(cls, self, *, ctx):
        me = markup.elements

        body = []
        body.append(me.doc.Text(text='VALUE: {!r}'.format(self.value)))

        return me.lang.ExceptionContext(title=self.title, body=body)


class AtomValueError(PointerError, CorrectnessError, ValueError):
    def __init__(self, msg, *, details=None, pointer=None, source=None, value=None):
        super().__init__(msg, details=details, pointer=pointer, source=source)
        self.value = value
        if value is not None:
            _replace_context(self, AtomValueErrorContext(value))


class LinkTargetError(PointerError, CorrectnessError, TypeError):
    def __init__(self, msg, *, details=None, pointer=None, source=None,
                               expected_types=None, received_type):
        super().__init__(msg, details=details, pointer=pointer, source=source)
        self.expected_types = expected_types
        self.received_type = received_type


class AtomConstraintViolationError(AtomValueError):
    def __init__(self, msg, *, constraint, **kwargs):
        super().__init__(msg, **kwargs)
        self.constraint = constraint


class ExistenceError(CorrectnessError, ValueError):
    pass


class LinkExistenceError(PointerError, ExistenceError):
    pass


class LinkMappingCardinalityViolationError(PointerError, CorrectnessError, ValueError):
    pass


class PointerReferenceError(EdgeDBError, AttributeError):
    pass


class PointerConstraintViolationError(PointerError, CorrectnessError, ValueError):
    def __init__(self, msg, *, pointer, pointer_proto=None, source,
                               constraint, value=None, **kwargs):
        super().__init__(msg, pointer=pointer, pointer_proto=pointer_proto,
                         source=source, **kwargs)
        self.constraint = constraint
        self.value = value


class PointerConstraintUniqueViolationError(PointerConstraintViolationError):
    pass


class StorageError(EdgeDBError):
    pass


class UninterpretedStorageError(StorageError):
    pass


class SessionRequiredError(EdgeDBError):
    pass
