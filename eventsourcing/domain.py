import inspect
import os
from abc import ABC, ABCMeta
from dataclasses import dataclass
from datetime import datetime, tzinfo
from types import FunctionType, WrapperDescriptorType
from typing import (
    Any,
    Callable,
    Dict,
    Generic,
    Iterable,
    List,
    Optional,
    Set,
    Tuple,
    Type,
    TypeVar,
    Union,
)
from uuid import UUID, uuid4

from eventsourcing.utils import get_method_name, get_topic, resolve_topic

# noinspection SpellCheckingInspection
TZINFO: tzinfo = resolve_topic(os.getenv("TZINFO_TOPIC", "datetime:timezone.utc"))


class MetaDomainEvent(ABCMeta):
    def __new__(
        mcs, name: str, bases: Tuple[type, ...], cls_dict: Dict[str, Any]
    ) -> "MetaDomainEvent":
        event_cls = super().__new__(mcs, name, bases, cls_dict)
        event_cls = dataclass(frozen=True)(event_cls)  # type: ignore
        return event_cls


T = TypeVar("T")


@dataclass(frozen=True)
class DomainEvent(ABC, Generic[T]):
    """
    Base class for domain events, such as aggregate :class:`AggregateEvent`
    and aggregate :class:`Snapshot`.
    """

    originator_id: UUID
    originator_version: int
    timestamp: datetime

    def mutate(self, aggregate: Optional[T]) -> Optional[T]:
        """Abstract mutator method."""


TDomainEvent = TypeVar("TDomainEvent", bound=DomainEvent[Any])
TAggregate = TypeVar("TAggregate", bound="Aggregate")


class AggregateEvent(DomainEvent[TAggregate], metaclass=MetaDomainEvent):
    """
    Base class for aggregate events. Subclasses will model
    decisions made by the domain model aggregates.
    """

    def mutate(self, aggregate: Optional[TAggregate]) -> Optional[TAggregate]:
        """
        Changes the state of the aggregate
        according to domain event attributes.
        """
        # Check event is next in its sequence.
        # Use counting to follow the sequence.
        # assert isinstance(obj, Aggregate), (type(obj), self)
        assert aggregate is not None
        next_version = aggregate.version + 1
        if self.originator_id != aggregate.id:
            raise OriginatorIDError(self.originator_id, aggregate.id)
        if self.originator_version != next_version:
            raise OriginatorVersionError(self.originator_version, next_version)
        if self.apply(aggregate) is not None:  # type: ignore
            raise TypeError(
                f"Unexpected value returned from "
                f"{type(self).apply.__qualname__}(). Values "
                f"returned from 'apply' methods are discarded."
            )
        # Update the aggregate version.
        aggregate.version = self.originator_version
        # Update the modified time.
        aggregate.modified_on = self.timestamp
        return aggregate

    def apply(self, aggregate: TAggregate) -> None:
        """
        Applies the domain event to the aggregate.
        """


class AggregateCreated(AggregateEvent[TAggregate]):
    # noinspection PyUnresolvedReferences
    """
    Domain event for when aggregate is created.

    Constructor arguments:

    :param UUID originator_id: ID of originating aggregate.
    :param int originator_version: version of originating aggregate.
    :param datetime timestamp: date-time of the event
    :param str originator_topic: topic for the aggregate class
    """

    originator_topic: str

    def mutate(self, aggregate: Optional[TAggregate]) -> Optional[TAggregate]:
        """
        Constructs aggregate instance defined
        by domain event object attributes.
        """
        assert aggregate is None
        # Copy the event attributes.
        kwargs = self.__dict__.copy()

        # Resolve originator topic.
        aggregate_class: Type[TAggregate] = resolve_topic(
            kwargs.pop("originator_topic")
        )

        # Construct and return aggregate object.
        agg = aggregate_class.__new__(aggregate_class)

        # Separate the base class keywords arguments.
        base_kwargs = {
            "id": kwargs.pop("originator_id"),
            "version": kwargs.pop("originator_version"),
            "timestamp": kwargs.pop("timestamp"),
        }

        # Call the base class init method.
        agg.__base_init__(**base_kwargs)

        # Provide the id, if the init method expects it.
        if aggregate_class in _init_mentions_id:
            kwargs["id"] = base_kwargs["id"]

        # Call the aggregate class init method.
        # noinspection PyArgumentList
        agg.__init__(**kwargs)
        return agg


class CommandMethodDecorator:
    def __init__(
        self, arg: Union[Callable[[Any], Any], str, Type[AggregateEvent[Any]]]
    ):
        self.is_name_inferred_from_method = False
        self.given_event_cls: Optional[Type[AggregateEvent[Any]]] = None
        self.event_cls_name: Optional[str] = None
        self.is_property_setter = False
        self.property_setter_arg_name: Optional[str] = None
        self.is_decorating_a_property = False
        self.decorated_property: Optional[property] = None
        self.original_method: Optional[FunctionType] = None
        # Initialising an instance.
        if isinstance(arg, str):
            # Decorator used with an explicit name.
            self.initialise_from_explicit_name(event_cls_name=arg)
        elif isinstance(arg, type) and issubclass(arg, AggregateEvent):
            self.initialise_from_event_cls(event_cls=arg)
        elif isinstance(arg, FunctionType):
            # Decorator used without explicit name.
            self.initialise_from_decorated_method(original_method=arg)
        elif isinstance(arg, property):
            method_name = arg.fset.__name__
            raise TypeError(
                f"@event on {method_name}() property setter requires event class name"
            )
        elif isinstance(arg, staticmethod):
            raise TypeError(
                f"{arg.__func__.__name__}() static method can't be "
                f"used to update aggregate state"
            )
        elif isinstance(arg, classmethod):
            raise TypeError(
                f"{arg.__func__.__name__}() class method can't be "
                f"used to update aggregate state"
            )
        else:
            raise TypeError(f"Unsupported usage: {type(arg)} is not a str or function")

    def initialise_from_decorated_method(self, original_method: FunctionType) -> None:
        self.original_method = original_method
        original_method_name = original_method.__name__
        if original_method_name != "__init__":
            self.is_name_inferred_from_method = True
            self.event_cls_name = "".join(
                [s.capitalize() for s in original_method_name.split("_")]
            )
        _check_no_variable_params(self.original_method)

    def initialise_from_event_cls(self, event_cls: Type[AggregateEvent[Any]]) -> None:
        self.given_event_cls = event_cls

    def initialise_from_explicit_name(self, event_cls_name: str) -> None:
        if event_cls_name == "":
            raise ValueError("Can't use empty string as name of event class")
        self.event_cls_name = event_cls_name

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        # Calling an instance.
        # noinspection SpellCheckingInspection
        if self.original_method is None:
            # Decorator doesn't yet know what method is being decorated,
            # so decorator must have been specified with an explicit
            # event name or class, so we're still initialising...
            assert len(kwargs) == 0, "Unsupported usage"
            assert len(args) == 1, "Unsupported usage"
            arg = args[0]
            # assert isinstance(args[0], FunctionType), args[0]
            if isinstance(arg, FunctionType):
                # Decorating a function.
                self.original_method = arg
                _check_no_variable_params(self.original_method)
            elif isinstance(arg, property):
                # Decorating a property.
                self.is_decorating_a_property = True
                self.decorated_property = arg
                if arg.fset is None:
                    assert arg.fget is not None
                    method_name = arg.fget.__name__
                    raise TypeError(
                        f"@event can't decorate {method_name}() property getter"
                    )
                assert isinstance(arg.fset, FunctionType)
                self.original_method = arg.fset
                assert self.original_method
                setter_arg_names = list(inspect.signature(arg.fset).parameters)
                assert len(setter_arg_names) == 2
                self.property_setter_arg_name = setter_arg_names[1]
                _check_no_variable_params(self.original_method)
            else:
                raise ValueError(
                    f"Unsupported usage: {type(arg)} is not a str or a FunctionType"
                )
            if self.given_event_cls:
                if self.given_event_cls in original_methods:
                    name = self.given_event_cls.__name__
                    raise TypeError(
                        f"{name} event class used in more than one decorator"
                    )

                # Set decorated event apply() method on given event class.
                if "apply" in self.given_event_cls.__dict__:
                    name = self.given_event_cls.__name__
                    raise TypeError(f"{name} event class has unexpected apply() method")
                # self.given_event_cls.apply = DecoratedEvent.apply  # type: ignore
                setattr(  # noqa: B010
                    self.given_event_cls, "apply", DecoratedEvent.apply
                )
                # Register the decorated method under the given event class.
                original_methods[self.given_event_cls] = self.original_method
            return self
        else:
            # Initialised decorator was called directly, presumably by
            # a decorating property that has this decorator as its fset.
            # So trigger an event.
            assert self.is_property_setter
            assert self.property_setter_arg_name
            assert len(args) == 2
            assert len(kwargs) == 0
            assert isinstance(args[0], Aggregate)
            aggregate_instance = args[0]
            bound = BoundCommandMethodDecorator(self, aggregate_instance)
            property_setter_arg_value = args[1]
            kwargs = {self.property_setter_arg_name: property_setter_arg_value}
            bound.trigger(**kwargs)

    def __get__(
        self, instance: Optional["Aggregate"], owner: "MetaAggregate"
    ) -> Union["BoundCommandMethodDecorator", "UnboundCommandMethodDecorator"]:
        if self.is_decorating_a_property:
            assert self.decorated_property
            return self.decorated_property.__get__(instance, owner)
        else:
            if instance is None:
                return UnboundCommandMethodDecorator(self)
            else:
                return BoundCommandMethodDecorator(self, instance)

    def __set__(self, instance: "Aggregate", value: Any) -> None:
        assert self.is_decorating_a_property
        # Set decorated property.
        b = BoundCommandMethodDecorator(self, instance)
        assert self.property_setter_arg_name
        kwargs = {self.property_setter_arg_name: value}
        b.trigger(**kwargs)


def event(
    arg: Optional[Union[FunctionType, str, Type[AggregateEvent[Any]]]] = None
) -> CommandMethodDecorator:
    """
    Can be used to decorate an aggregate method so that when the
    method is called an event is triggered. The body of the method
    will be used to apply the event to the aggregate, both when the
    event is triggered and when the aggregate is reconstructed from
    stored events.

    .. code-block:: python

        class MyAggregate(Aggregate):
            @event("NameChanged")
            def set_name(self, name: str):
                self.name = name

    ...is equivalent to...

    .. code-block:: python

        class MyAggregate(Aggregate):
            def set_name(self, name: str):
                self.trigger_event(self.NameChanged, name=name)

            class NameChanged(Aggregate.Event):
                name: str

                def apply(self, aggregate):
                    aggregate.name = self.name

    In the example above, the event "NameChanged" is defined automatically
    by inspecting the signature of the `set_name()` method. If it is
    preferred to declare the event class explicitly, for example to define
    upcasting of old events, the event class itself can be mentioned in the
    event decorator rather than just providing the name of the event as a
    string.

    .. code-block:: python

        class MyAggregate(Aggregate):

            class NameChanged(Aggregate.Event):
                name: str

            @event(NameChanged)
            def set_name(self, name: str):
                aggregate.name = self.name


    """
    if arg is None:
        return event  # type: ignore
    else:
        return CommandMethodDecorator(arg)


triggers = event


class UnboundCommandMethodDecorator:
    """
    Wraps an EventDecorator instance when attribute is accessed
    on an aggregate class.
    """

    def __init__(self, event_decorator: CommandMethodDecorator):
        """

        :param CommandMethodDecorator event_decorator:
        """
        self.event_decorator = event_decorator
        assert event_decorator.original_method
        self.__qualname__ = event_decorator.original_method.__qualname__
        self.__name__ = event_decorator.original_method.__name__


class BoundCommandMethodDecorator:
    """
    Wraps an EventDecorator instance when attribute is accessed
    on an aggregate so that the aggregate methods can be accessed.
    """

    def __init__(
        self, event_decorator: CommandMethodDecorator, aggregate: "TAggregate"
    ):
        """

        :param CommandMethodDecorator event_decorator:
        :param Aggregate aggregate:
        """
        assert event_decorator.original_method
        self.event_decorator = event_decorator
        self.__qualname__ = event_decorator.original_method.__qualname__
        self.__name__ = event_decorator.original_method.__name__
        self.aggregate = aggregate

    def trigger(self, *args: Any, **kwargs: Any) -> None:
        assert isinstance(self.event_decorator, CommandMethodDecorator)  # for PyCharm
        assert self.event_decorator.original_method
        kwargs = _coerce_args_to_kwargs(
            self.event_decorator.original_method, args, kwargs
        )
        if self.event_decorator.given_event_cls:
            event_cls = self.event_decorator.given_event_cls
        else:
            assert self.event_decorator.event_cls_name
            event_cls = getattr(self.aggregate, self.event_decorator.event_cls_name)
        self.aggregate.trigger_event(event_cls, **kwargs)

    def __call__(self, *args: Any, **kwargs: Any) -> None:
        self.trigger(*args, **kwargs)


original_methods: Dict[MetaDomainEvent, FunctionType] = {}


class DecoratedEvent(AggregateEvent[Any]):
    def apply(self, aggregate: "TAggregate") -> None:
        """
        Applies event to aggregate by calling
        method decorated by @event.
        """
        event_obj_dict = dict(self.__dict__)
        event_obj_dict.pop("originator_id")
        event_obj_dict.pop("originator_version")
        event_obj_dict.pop("timestamp")
        original_method = original_methods[type(self)]
        method_signature = inspect.signature(original_method)
        # args = []
        # for name, param in method_signature.parameters.items():
        for name in method_signature.parameters:
            if name == "self":
                continue
        #     if param.kind == param.POSITIONAL_ONLY:
        #         args.append(event_obj_dict.pop(name))
        # original_method(aggregate, *args, **event_obj_dict)
        returned_value = original_method(aggregate, **event_obj_dict)
        if returned_value is not None:
            raise TypeError(
                f"Unexpected value returned from "
                f"{original_method.__qualname__}(). Values "
                f"returned from 'apply' methods are discarded."
            )


def _check_no_variable_params(
    method: Union[FunctionType, WrapperDescriptorType]
) -> None:
    assert isinstance(method, (FunctionType, WrapperDescriptorType)), type(method)
    for param in inspect.signature(method).parameters.values():
        if param.kind is param.VAR_POSITIONAL:
            raise TypeError("variable positional parameters not supported")
            # Todo: Support VAR_POSITIONAL?
            # annotations["__star_args__"] = "typing.Any"

        elif param.kind is param.VAR_KEYWORD:
            # Todo: Support VAR_KEYWORD?
            # annotations["__star_kwargs__"] = "typing.Any"
            raise TypeError("variable keyword parameters not supported")


def _coerce_args_to_kwargs(
    method: Union[FunctionType, WrapperDescriptorType],
    args: Iterable[Any],
    kwargs: Dict[str, Any],
    expects_id: bool = False,
) -> Dict[str, Any]:
    assert isinstance(method, (FunctionType, WrapperDescriptorType))
    method_signature = inspect.signature(method)
    copy_kwargs = dict(kwargs)
    args = tuple(args)
    positional_names = []
    keyword_defaults = {}
    required_positional = []
    required_keyword_only = []

    if expects_id:
        positional_names.append("id")
        required_positional.append("id")
    for name, param in method_signature.parameters.items():
        if name == "self":
            continue
        # elif param.kind in (param.POSITIONAL_ONLY, param.POSITIONAL_OR_KEYWORD):
        if param.kind is param.KEYWORD_ONLY:
            required_keyword_only.append(name)
        if param.kind is param.POSITIONAL_OR_KEYWORD:
            positional_names.append(name)
            if param.default == param.empty:
                required_positional.append(name)
        if param.default != param.empty:
            keyword_defaults[name] = param.default

    # if not required_keyword_only and not positional_names:
    #     if args or kwargs:
    #         raise TypeError(f"{method.__name__}() takes no args")

    for name in kwargs:
        if name not in required_keyword_only and name not in positional_names:
            raise TypeError(
                f"{get_method_name(method)}() got an unexpected "
                f"keyword argument '{name}'"
            )

    counter = 0
    len_args = len(args)
    if len_args > len(positional_names):
        msg = (
            f"{get_method_name(method)}() takes {len(positional_names) + 1} "
            f"positional argument{'' if len(positional_names) + 1 == 1 else 's'} "
            f"but {len_args + 1} were given"
        )
        raise TypeError(msg)

    required_positional_not_in_kwargs = [
        n for n in required_positional if n not in kwargs
    ]
    num_missing = len(required_positional_not_in_kwargs) - len_args
    if num_missing > 0:
        missing_names = [
            f"'{name}'" for name in required_positional_not_in_kwargs[len_args:]
        ]
        msg = (
            f"{get_method_name(method)}() missing {num_missing} required positional "
            f"argument{'' if num_missing == 1 else 's'}: "
        )
        raise_missing_names_type_error(missing_names, msg)

    for name in positional_names:
        if counter + 1 > len_args:
            break
        if name not in kwargs:
            copy_kwargs[name] = args[counter]
            counter += 1
        else:
            raise TypeError(
                f"{get_method_name(method)}() got multiple values for argument '{name}'"
            )

    missing_keyword_only_arguments = []
    for name in required_keyword_only:
        if name not in kwargs:
            missing_keyword_only_arguments.append(name)

    if missing_keyword_only_arguments:
        missing_names = [f"'{name}'" for name in missing_keyword_only_arguments]
        msg = (
            f"{get_method_name(method)}() missing {len(missing_names)} "
            f"required keyword-only argument"
            f"{'' if len(missing_names) == 1 else 's'}: "
        )
        raise_missing_names_type_error(missing_names, msg)

    for name, value in keyword_defaults.items():
        if name not in copy_kwargs:
            copy_kwargs[name] = value
    return copy_kwargs


def raise_missing_names_type_error(missing_names: List[str], msg: str) -> None:
    msg += missing_names[0]
    if len(missing_names) == 2:
        msg += f" and {missing_names[1]}"
    elif len(missing_names) > 2:
        msg += ", " + ", ".join(missing_names[1:-1])
        msg += f", and {missing_names[-1]}"
    raise TypeError(msg)


TT = TypeVar("TT", bound="type")

_annotations_mention_id: Set["MetaAggregate"] = set()
_init_mentions_id: Set["MetaAggregate"] = set()


class MetaAggregate(ABCMeta):
    INITIAL_VERSION = 1

    def __new__(mcs: Type[TT], *args: Any, **kwargs: Any) -> TT:
        try:
            args[2]["__annotations__"].pop("id")
        except KeyError:
            annotations_mention_id = False
        else:
            annotations_mention_id = True
        cls = ABCMeta.__new__(mcs, *args)
        cls = dataclass(eq=False, repr=False)(cls)
        if annotations_mention_id:
            _annotations_mention_id.add(cls)
        return cls

    def __init__(
        cls,
        *args: Any,
        created_event_name: Optional[str] = None,
    ) -> None:
        super().__init__(*args)

        # Prepare created event class.
        created_event_classes = {}

        try:
            created_event_class = cls.__dict__["_created_event_class"]
            if created_event_name:
                raise TypeError(
                    "Can't use both '_created_event_class' and 'created_event_name'"
                )
        except KeyError:
            created_event_class = None

        if isinstance(cls.__dict__["__init__"], CommandMethodDecorator):
            init_decorator: CommandMethodDecorator = cls.__dict__["__init__"]
            init_method = init_decorator.original_method
            if created_event_name:
                raise TypeError(
                    "Can't use both 'created_event_name' and __init__ @event decorator"
                )
            elif created_event_class:
                raise TypeError(
                    "Can't use both '_created_event_class' and __init__ @event "
                    "decorator"
                )
            elif init_decorator.event_cls_name:
                created_event_name = init_decorator.event_cls_name
            elif init_decorator.given_event_cls:
                created_event_class = init_decorator.given_event_cls
            else:
                raise TypeError(
                    "Neither name nor class given to __init__ @event decorator"
                )
            cls.__init__ = init_method  # type: ignore
        else:
            init_method = cls.__dict__["__init__"]

        assert isinstance(init_method, FunctionType)

        for name, value in tuple(cls.__dict__.items()):
            if isinstance(value, type) and issubclass(value, AggregateCreated):
                created_event_classes[name] = value

        # Use the class as the created class, if so named.
        if created_event_name in created_event_classes:
            created_event_class = created_event_classes[created_event_name]

        elif created_event_class is None:
            if len(created_event_classes) == 0 or created_event_name:
                if not created_event_name:
                    created_event_name = "Created"
                # Define a "created" event for this class.
                created_cls_annotations = {}
                _check_no_variable_params(init_method)
                method_signature = inspect.signature(init_method)
                for param_name in method_signature.parameters:
                    if param_name == "self":
                        continue
                    if param_name == "id":
                        _init_mentions_id.add(cls)
                        continue
                    created_cls_annotations[param_name] = "typing.Any"

                created_event_class = type(
                    created_event_name,
                    (AggregateCreated,),
                    {
                        "__annotations__": created_cls_annotations,
                        "__module__": cls.__module__,
                        "__qualname__": ".".join(
                            [cls.__qualname__, created_event_name]
                        ),
                    },
                )
                setattr(cls, created_event_name, created_event_class)

            elif len(created_event_classes) == 1:
                created_event_class = list(created_event_classes.values())[0]

        cls._created_event_class = created_event_class

        # Prepare the subsequent event classes.
        for attribute in tuple(cls.__dict__.values()):

            # Watch out for @property that sits over an @event.
            if isinstance(attribute, property) and isinstance(
                attribute.fset, CommandMethodDecorator
            ):
                attribute = attribute.fset
                if attribute.is_name_inferred_from_method:
                    # We don't want name inferred from property (not past participle).
                    method_name = attribute.original_method.__name__
                    raise TypeError(
                        f"@event under {method_name}() property setter requires event "
                        f"class name"
                    )
                # Attribute is a property decorating an event decorator.
                attribute.is_property_setter = True

            # Attribute is an event decorator.
            if isinstance(attribute, CommandMethodDecorator):
                # Prepare the subsequent aggregate events.
                original_method = attribute.original_method
                assert isinstance(original_method, FunctionType)

                method_signature = inspect.signature(original_method)
                annotations = {}
                for param_name in method_signature.parameters:
                    if param_name == "self":
                        continue
                    elif attribute.is_property_setter:
                        assert len(method_signature.parameters) == 2
                        attribute.property_setter_arg_name = param_name
                    annotations[param_name] = "typing.Any"  # Todo: Improve this?

                if not attribute.given_event_cls:
                    assert attribute.event_cls_name
                    event_cls_name = attribute.event_cls_name

                    # Check event class isn't already defined.
                    if event_cls_name in cls.__dict__:
                        raise TypeError(
                            f"{event_cls_name} event already defined on {cls.__name__}"
                        )

                    event_cls_qualname = ".".join([cls.__qualname__, event_cls_name])
                    event_cls_dict = {
                        "__annotations__": annotations,
                        "__module__": cls.__module__,
                        "__qualname__": event_cls_qualname,
                    }
                    event_cls = MetaDomainEvent(
                        event_cls_name, (DecoratedEvent,), event_cls_dict
                    )
                    original_methods[event_cls] = original_method
                    setattr(cls, event_cls_name, event_cls)
        # Inspect the parameters of the create_id method.
        cls._create_id_param_names = []
        for name, param in inspect.signature(cls.create_id).parameters.items():
            if param.kind in [param.KEYWORD_ONLY, param.POSITIONAL_OR_KEYWORD]:
                cls._create_id_param_names.append(name)

    def __call__(cls: "MetaAggregate", *args: Any, **kwargs: Any) -> Any:
        self_init: WrapperDescriptorType = cls.__init__  # type: ignore
        kwargs = _coerce_args_to_kwargs(
            self_init,
            args,
            kwargs,
            expects_id=cls in _annotations_mention_id,
        )
        if cls._created_event_class is None:
            raise TypeError("attribute '_created_event_class' not set on class")
        else:
            return cls._create(
                event_class=cls._created_event_class,
                **kwargs,
            )

    def _create(
        cls,
        event_class: Type[AggregateCreated[TAggregate]],
        *,
        id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> TAggregate:
        """
        Factory method to construct a new
        aggregate object instance.
        """
        # Construct the domain event class,
        # with an ID and version, and the
        # a topic for the aggregate class.
        create_id_kwargs = {
            k: v for k, v in kwargs.items() if k in cls._create_id_param_names
        }

        try:
            # noinspection PyArgumentList
            created_event = event_class(  # type: ignore
                originator_topic=get_topic(cls),
                originator_id=id or cls.create_id(**create_id_kwargs),
                originator_version=cls.INITIAL_VERSION,
                timestamp=datetime.now(tz=TZINFO),
                **kwargs,
            )
        except TypeError as e:
            msg = (
                f"Unable to construct 'aggregate created' "
                f"event with class {event_class.__qualname__} "
                f"and keyword args {kwargs}: {e}"
            )
            raise TypeError(msg)
        # Construct the aggregate object.
        agg = created_event.mutate(None)

        assert agg is not None
        # Append the domain event to pending list.
        agg.pending_events.append(created_event)
        # Return the aggregate.
        return agg

    # noinspection PyUnusedLocal
    @staticmethod
    def create_id(**kwargs: Any) -> UUID:
        """
        Returns a new aggregate ID.
        """
        return uuid4()


class Aggregate(ABC, metaclass=MetaAggregate):
    """
    Base class for aggregate roots.
    """

    def __base_init__(self, id: UUID, version: int, timestamp: datetime) -> None:
        """
        Initialises an aggregate object with an :data:`id`, a :data:`version`
        number, and a :data:`timestamp`.
        """
        self._id = id
        self._version = version
        self._created_on = timestamp
        self._modified_on = timestamp
        self._pending_events: List[AggregateEvent[Any]] = []

    @property
    def id(self) -> UUID:
        """
        The ID of the aggregate.
        """
        return self._id

    @property
    def version(self) -> int:
        """
        The version number of the aggregate.
        """
        return self._version

    @version.setter
    def version(self, version: int) -> None:
        # noinspection PyAttributeOutsideInit
        self._version = version

    @property
    def created_on(self) -> datetime:
        """
        The date and time when the aggregate was created.
        """
        return self._created_on

    @property
    def modified_on(self) -> datetime:
        """
        The date and time when the aggregate was last modified.
        """
        return self._modified_on

    @modified_on.setter
    def modified_on(self, modified_on: datetime) -> None:
        # noinspection PyAttributeOutsideInit
        self._modified_on = modified_on

    @property
    def pending_events(self) -> List[AggregateEvent[Any]]:
        """
        A list of pending events.
        """
        return self._pending_events

    class Event(AggregateEvent[TAggregate]):
        pass

    class Created(Event[TAggregate], AggregateCreated[TAggregate]):
        pass

    def __new__(cls, *args: Any, **kwargs: Any) -> Any:
        return object.__new__(cls)

    def __eq__(self, other: Any) -> bool:
        return type(self) == type(other) and self.__dict__ == other.__dict__

    def __repr__(self) -> str:
        attrs = [
            f"{k.lstrip('_')}={v!r}"
            for k, v in self.__dict__.items()
            if k != "_pending_events"
        ]
        return f"{type(self).__name__}({', '.join(attrs)})"

    def trigger_event(
        self,
        event_class: Type[AggregateEvent[Any]],
        **kwargs: Any,
    ) -> None:
        """
        Triggers domain event of given type, by creating
        an event object and using it to mutate the aggregate.
        """
        # Construct the domain event as the
        # next in the aggregate's sequence.
        # Use counting to generate the sequence.
        next_version = self.version + 1
        try:
            new_event = event_class(  # type: ignore
                originator_id=self.id,
                originator_version=next_version,
                timestamp=datetime.now(tz=TZINFO),
                **kwargs,
            )
        except TypeError as e:
            raise TypeError(f"Can't construct event {event_class}: {e}")

        # Mutate aggregate with domain event.
        new_event.mutate(self)
        # Append the domain event to pending list.
        self.pending_events.append(new_event)

    def collect_events(self) -> List[AggregateEvent[Any]]:
        """
        Collects and returns a list of pending aggregate
        :class:`AggregateEvent` objects.
        """
        collected = []
        while self.pending_events:
            collected.append(self.pending_events.pop(0))
        return collected


# @overload
# def aggregate(*, created_event_name: str) -> Callable[[Any], Type[Aggregate]]:
#     ...
#
#
# @overload
# def aggregate(cls: Any) -> Type[Aggregate]:
#     ...


def aggregate(
    cls: Optional[Any] = None,
    *,
    created_event_name: Optional[str] = None,
) -> Union[Type[Aggregate], Callable[[Any], Type[Aggregate]]]:
    """
    Converts the class that was passed in to inherit from Aggregate.

    .. code-block:: python

        @aggregate
        class MyAggregate:
            pass

    ...is equivalent to...

    .. code-block:: python

        class MyAggregate(Aggregate):
            pass
    """

    def decorator(cls_: Any) -> Type[Aggregate]:
        if issubclass(cls_, Aggregate):
            raise TypeError(f"{cls_.__name__} is already an Aggregate")
        bases = cls_.__bases__
        if bases == (object,):
            bases = (Aggregate,)
        else:
            bases += (Aggregate,)
        cls_dict = dict()
        cls_dict.update(cls_.__dict__)
        cls_ = MetaAggregate(
            cls_.__name__,
            bases,
            cls_dict,
            created_event_name=created_event_name,
        )
        assert issubclass(cls_, Aggregate)
        return cls_

    if cls:
        return decorator(cls)
    else:
        return decorator


class OriginatorIDError(Exception):
    """
    Raised when a domain event can't be applied to
    an aggregate due to an ID mismatch indicating
    the domain event is not in the aggregate's
    sequence of events.
    """


class OriginatorVersionError(Exception):
    """
    Raised when a domain event can't be applied to
    an aggregate due to version mismatch indicating
    the domain event is not the next in the aggregate's
    sequence of events.
    """


class VersionError(OriginatorVersionError):
    """
    Old name for 'OriginatorVersionError'.

    This class exists to maintain backwards-compatibility
    but will be removed in a future version Please use
    'OriginatorVersionError' instead.
    """


class Snapshot(DomainEvent["Aggregate"], metaclass=MetaDomainEvent):
    # noinspection PyUnresolvedReferences
    """
    Snapshots represent the state of an aggregate at a particular
    version.

    Constructor arguments:

    :param UUID originator_id: ID of originating aggregate.
    :param int originator_version: version of originating aggregate.
    :param datetime timestamp: date-time of the event
    :param str topic: string that includes a class and its module
    :param dict state: version of originating aggregate.
    """

    topic: str
    state: Dict[str, Any]

    @classmethod
    def take(cls, aggregate: Aggregate) -> "Snapshot":
        """
        Creates a snapshot of the given :class:`Aggregate` object.
        """
        aggregate_state = dict(aggregate.__dict__)
        aggregate_state.pop("_pending_events")
        class_version = getattr(type(aggregate), "class_version", 1)
        if class_version > 1:
            aggregate_state["class_version"] = class_version
        originator_id = aggregate_state.pop("_id")
        originator_version = aggregate_state.pop("_version")
        # noinspection PyArgumentList
        return cls(  # type: ignore
            originator_id=originator_id,
            originator_version=originator_version,
            timestamp=datetime.now(tz=TZINFO),
            topic=get_topic(type(aggregate)),
            state=aggregate_state,
        )

    def mutate(self, aggregate: Optional[Aggregate]) -> Optional[Aggregate]:
        """
        Reconstructs the snapshotted :class:`Aggregate` object.
        """
        cls = resolve_topic(self.topic)
        assert issubclass(cls, Aggregate)
        aggregate_state = dict(self.state)
        from_version = aggregate_state.pop("class_version", 1)
        class_version = getattr(cls, "class_version", 1)
        while from_version < class_version:
            upcast_name = f"upcast_v{from_version}_v{from_version + 1}"
            upcast = getattr(cls, upcast_name)
            upcast(aggregate_state)
            from_version += 1

        aggregate_state["_id"] = self.originator_id
        aggregate_state["_version"] = self.originator_version
        aggregate_state["_pending_events"] = []
        aggregate = object.__new__(cls)
        aggregate.__dict__.update(aggregate_state)
        return aggregate
