from __future__ import annotations

import importlib
import sys
from inspect import signature
from pathlib import Path
from types import FunctionType
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Optional,
    Tuple,
    Union,
)
from warnings import warn

from magicgui import magicgui
from napari_plugin_engine import HookImplementation
from napari_plugin_engine import PluginManager as _PM
from numpy import isin
from typing_extensions import TypedDict

from ..types import AugmentedWidget, LayerData, SampleDict
from ..utils._appdirs import user_site_packages
from ..utils.misc import camel_to_spaces, running_as_bundled_app
from ..utils.translations import trans
from . import _builtins, hook_specifications

if sys.platform.startswith('linux') and running_as_bundled_app():
    sys.path.append(user_site_packages())


if TYPE_CHECKING:
    from magicgui.widgets import FunctionGui
    from qtpy.QtWidgets import QWidget

    from ..utils.settings._defaults import CallOrderDict


class PluginManager(_PM):
    def call_order(self) -> CallOrderDict:
        """Returns the call order from the plugin manager.

        Returns
        -------
        call_order : CallOrderDict
        call_order =
            {
                        spec_name: [
                                {
                                    name: plugin_name
                                    enabled: enabled
                                },
                                {
                                    name: plugin_name
                                    enabled: enabled
                                },
                                ...
                        ],
                        ...
            }

        """

        order = {}
        for spec_name, caller in self.hooks.items():
            # no need to save call order unless we only use first result
            if not caller.is_firstresult:
                continue
            impls = caller.get_hookimpls()
            # no need to save call order if there is only a single item
            if len(impls) > 1:
                order[spec_name] = [
                    {'plugin': impl.plugin_name, 'enabled': impl.enabled}
                    for impl in reversed(impls)
                ]
        return order

    def set_call_order(self, new_order: CallOrderDict):
        """Sets the plugin manager call order to match SETTINGS plugin values.

        Note: Run this after load_settings_plugin_defaults, which
        sets the default values in SETTINGS.

        Parameters
        ----------
        new_order : CallOrderDict

        Examples
        --------
        >>> new_order =
        ...         {
        ...             spec_name: [
        ...                     {
        ...                         name: plugin_name
        ...                         enabled: enabled
        ...                     },
        ...                     {
        ...                         name: plugin_name
        ...                         enabled: enabled
        ...                     },
        ...                     ...
        ...             ],
        ...             ...
        ...         }
        ... plugin_manager.set_call_order(new_order)
        """

        for spec_name, hook_caller in self.hooks.items():
            order = []
            for p in new_order.get(spec_name, []):
                try:
                    # if the plugin was uninstalled in the meantime,
                    # we should just move on to the next plugin
                    imp = hook_caller.get_plugin_implementation(p['plugin'])
                except KeyError:
                    continue
                imp.enabled = p['enabled']
                order.append(p['plugin'])
            if order:
                hook_caller.bring_to_front(order)


# the main plugin manager instance for the `napari` plugin namespace.
plugin_manager = PluginManager('napari', discover_entry_point='napari.plugin')
with plugin_manager.discovery_blocked():
    plugin_manager.add_hookspecs(hook_specifications)
    plugin_manager.register(_builtins, name='builtins')
    if importlib.util.find_spec("skimage") is not None:
        from . import _skimage_data

        plugin_manager.register(_skimage_data, name='scikit-image')

WidgetCallable = Callable[..., Union['FunctionGui', 'QWidget']]
dock_widgets: Dict[
    str, Dict[str, Tuple[WidgetCallable, Dict[str, Any]]]
] = dict()
function_widgets: Dict[str, Dict[str, Callable[..., Any]]] = dict()
_sample_data: Dict[str, Dict[str, SampleDict]] = dict()


def register_dock_widget(
    args: Union[AugmentedWidget, List[AugmentedWidget]],
    hookimpl: HookImplementation,
):
    from qtpy.QtWidgets import QWidget

    plugin_name = hookimpl.plugin_name
    hook_name = '`napari_experimental_provide_dock_widget`'
    for arg in args if isinstance(args, list) else [args]:
        if isinstance(arg, tuple):
            if not arg:
                warn(
                    trans._(
                        'Plugin {plugin_name!r} provided an invalid tuple to {hook_name}. Skipping',
                        deferred=True,
                        plugin_name=plugin_name,
                        hook_name=hook_name,
                    )
                )
                continue
            _cls = arg[0]
            kwargs = arg[1] if len(arg) > 1 else {}
        else:
            _cls, kwargs = (arg, {})

        if not callable(_cls):
            warn(
                trans._(
                    'Plugin {plugin_name!r} provided a non-callable object (widget) to {hook_name}: {_cls!r}. Widget ignored.',
                    deferred=True,
                    plugin_name=plugin_name,
                    hook_name=hook_name,
                    _cls=_cls,
                )
            )
            continue

        if not isinstance(kwargs, dict):
            warn(
                trans._(
                    'Plugin {plugin_name!r} provided invalid kwargs to {hook_name} for class {class_name}. Widget ignored.',
                    deferred=True,
                    plugin_name=plugin_name,
                    hook_name=hook_name,
                    class_name=_cls.__name__,
                )
            )
            continue

        # Get widget name
        name = str(kwargs.get('name', '')) or camel_to_spaces(_cls.__name__)

        if plugin_name not in dock_widgets:
            # tried defaultdict(dict) but got odd KeyErrors...
            dock_widgets[plugin_name] = {}
        elif name in dock_widgets[plugin_name]:
            warn(
                trans._(
                    "Plugin '{plugin_name}' has already registered a dock widget '{name}' which has now been overwritten",
                    deferred=True,
                    plugin_name=plugin_name,
                    name=name,
                )
            )

        dock_widgets[plugin_name][name] = (_cls, kwargs)


def get_plugin_widget(
    plugin_name: str, widget_name: Optional[str] = None
) -> Tuple[WidgetCallable, Dict[str, Any]]:
    """Get widget `widget_name` provided by plugin `plugin_name`.

    Note: it's important that :func:`discover_dock_widgets` has been called
    first, otherwise plugins may not be found yet.  (Typically, that is done
    in qt_main_window)

    Parameters
    ----------
    plugin_name : str
        Name of a plugin providing a widget
    widget_name : str, optional
        Name of a widget provided by `plugin_name`. If `None`, and the
        specified plugin provides only a single widget, that widget will be
        returned, otherwise a ValueError will be raised, by default None

    Returns
    -------
    plugin_widget : Tuple[Callable, dict]
        Tuple of (widget_class, options).

    Raises
    ------
    KeyError
        If plugin `plugin_name` does not provide any widgets
    KeyError
        If plugin does not provide a widget named `widget_name`.
    ValueError
        If `widget_name` is not provided, but `plugin_name` provides more than
        one widget
    """
    plg_wdgs = dock_widgets.get(plugin_name)
    if not plg_wdgs:
        raise KeyError(
            trans._(
                'Plugin {plugin_name!r} does not provide any dock widgets',
                deferred=True,
                plugin_name=plugin_name,
            )
        )

    if not widget_name:
        if len(plg_wdgs) > 1:
            raise ValueError(
                trans._(
                    'Plugin {plugin_name!r} provides more than 1 dock_widget. Must also provide "widget_name" from {widgets}',
                    deferred=True,
                    plugin_name=plugin_name,
                    widgets=set(plg_wdgs),
                )
            )
        widget_name = list(plg_wdgs)[0]
    else:
        if widget_name not in plg_wdgs:
            raise KeyError(
                trans._(
                    'Plugin {plugin_name!r} does not provide a widget named {widget_name!r}',
                    deferred=True,
                    plugin_name=plugin_name,
                    widget_name=widget_name,
                )
            )
    return plg_wdgs[widget_name]


_magicgui_sig = {
    name
    for name, p in signature(magicgui).parameters.items()
    if p.kind is p.KEYWORD_ONLY
}


def register_function_widget(
    args: Union[Callable, List[Callable]],
    hookimpl: HookImplementation,
):
    plugin_name = hookimpl.plugin_name
    hook_name = '`napari_experimental_provide_function`'
    for func in args if isinstance(args, list) else [args]:
        if not isinstance(func, FunctionType):
            msg = [
                trans._(
                    'Plugin {plugin_name!r} provided a non-callable type to {hook_name}: {func_type!r}. Function widget ignored.',
                    deferred=True,
                    plugin_name=plugin_name,
                    hook_name=hook_name,
                    func_type=type(func),
                )
            ]
            if isinstance(func, tuple):
                msg.append(
                    trans._(
                        " To provide multiple function widgets please use a LIST of callables",
                        deferred=True,
                    )
                )

            warn("".join(msg))
            continue

        # Get function name
        name = func.__name__.replace('_', ' ')

        if plugin_name not in function_widgets:
            # tried defaultdict(dict) but got odd KeyErrors...
            function_widgets[plugin_name] = {}
        elif name in function_widgets[plugin_name]:
            warn(
                trans._(
                    "Plugin '{plugin_name}' has already registered a function widget '{name}' which has now been overwritten",
                    deferred=True,
                    plugin_name=plugin_name,
                    name=name,
                )
            )

        function_widgets[plugin_name][name] = func


def register_sample_data(
    data: Dict[str, Union[str, Callable[..., Iterable[LayerData]]]],
    hookimpl: HookImplementation,
):
    """Register sample data dict returned by `napari_provide_sample_data`.

    Each key in `data` is a `sample_name` (the string that will appear in the
    `Open Sample` menu), and the value is either a string, or a callable that
    returns an iterable of ``LayerData`` tuples, where each tuple is a 1-, 2-,
    or 3-tuple of ``(data,)``, ``(data, meta)``, or ``(data, meta,
    layer_type)``.

    Parameters
    ----------
    data : Dict[str, Union[str, Callable[..., Iterable[LayerData]]]]
        A mapping of {sample_name->data}
    hookimpl : HookImplementation
        The hook implementation that returned the dict
    """
    plugin_name = hookimpl.plugin_name
    hook_name = 'napari_provide_sample_data'
    if not isinstance(data, dict):
        warn(
            trans._(
                'Plugin {plugin_name!r} provided a non-dict object to {hook_name!r}: data ignored.',
                deferred=True,
                plugin_name=plugin_name,
                hook_name=hook_name,
            )
        )
        return

    _data = {}
    for name, datum in list(data.items()):
        if isinstance(datum, dict):
            if 'data' not in datum or 'display_name' not in datum:
                warn(
                    trans._(
                        'In {hook_name!r}, plugin {plugin_name!r} provided an invalid dict object for key {name!r} that does not have required keys: "data" and "display_name".  Ignoring',
                        deferred=True,
                        hook_name=hook_name,
                        plugin_name=plugin_name,
                        name=name,
                    )
                )
                continue
        else:
            datum = {'data': datum, 'display_name': name}

        if not (
            callable(datum['data']) or isinstance(datum['data'], (str, Path))
        ):
            warn(
                trans._(
                    'Plugin {plugin_name!r} provided invalid data for key {name!r} in the dict returned by {hook_name!r}. (Must be str, callable, or dict), got ({data_type}).',
                    deferred=True,
                    plugin_name=plugin_name,
                    name=name,
                    hook_name=hook_name,
                    data_type=type(datum["data"]),
                )
            )
            continue
        _data[name] = datum

    if plugin_name not in _sample_data:
        _sample_data[plugin_name] = {}

    _sample_data[plugin_name].update(_data)


def discover_dock_widgets():
    """Trigger discovery of dock_widgets plugins"""
    dw_hook = plugin_manager.hook.napari_experimental_provide_dock_widget
    dw_hook.call_historic(result_callback=register_dock_widget, with_impl=True)
    fw_hook = plugin_manager.hook.napari_experimental_provide_function
    fw_hook.call_historic(
        result_callback=register_function_widget, with_impl=True
    )


def discover_sample_data():
    """Trigger discovery of sample data."""
    sd_hook = plugin_manager.hook.napari_provide_sample_data
    sd_hook.call_historic(result_callback=register_sample_data, with_impl=True)


def available_samples() -> Tuple[Tuple[str, str], ...]:
    """Return a tuple of sample data keys provided by plugins.

    Returns
    -------
    sample_keys : Tuple[Tuple[str, str], ...]
        A sequence of 2-tuples ``(plugin_name, sample_name)`` showing available
        sample data provided by plugins.  To load sample data into the viewer,
        use :meth:`napari.Viewer.open_sample`.

    Examples
    --------
    .. code-block:: python

        from napari.plugins import available_samples

        sample_keys = available_samples()
        if sample_keys:
            # load first available sample
            viewer.open_sample(*sample_keys[0])
    """
    return tuple((p, s) for p in _sample_data for s in _sample_data[p])


discover_sample_data()


def load_settings_plugin_defaults(SETTINGS):
    """Sets SETTINGS plugin defaults on start up from the defaults saved in
    the plugin manager.
    """

    SETTINGS._defaults['plugins'].call_order = plugin_manager.call_order()


#: Template to use for namespacing a plugin item in the menu bar
menu_item_template = '{}: {}'

__all__ = [
    "PluginManager",
    "plugin_manager",
    'menu_item_template',
    'dock_widgets',
    'function_widgets',
    'available_samples',
]
