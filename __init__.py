# Dev reload
if "bpy" in locals():
    import sys
    print("Reloading Tracking Toolkit Modules")
    prefix = __package__ + "."
    for name in sys.modules.copy():
        if name.startswith(prefix):
            print(f"Reloading {name}")
            del sys.modules[name]

import bpy

from .tracking_toolkit.operators import (
    CreateRefsOperator,
    ToggleActiveOperator,
    ToggleRecordOperator,
    ConvertSubframesOperator,
)
from .tracking_toolkit.properties import (
    OVRContext,
    OVRTracker,
    OVRTarget,
    OVRTransform,
    OVRInput,
    Preferences,
)
from .tracking_toolkit.tracking import stop_preview
from .tracking_toolkit.ui import PANEL_UL_TrackerList, RecorderPanel


def scene_update_callback(scene: bpy.types.Scene, _):
    selected = [obj for obj in scene.objects if obj.select_get()]
    if not selected:
        return

    ovr_context = scene.OVRContext
    active = selected[-1].name
    for i, tracker in enumerate(ovr_context.trackers):
        target_obj = tracker.target.object
        if target_obj and target_obj.name == active:
            if ovr_context.selected_tracker != i:
                ovr_context.selected_tracker = i


def register():
    print("Loading Tracking Toolkit")

    bpy.utils.register_class(Preferences)
    bpy.utils.register_class(OVRTransform)
    bpy.utils.register_class(OVRTarget)
    bpy.utils.register_class(OVRTracker)
    bpy.utils.register_class(OVRInput)
    bpy.utils.register_class(OVRContext)

    bpy.utils.register_class(ToggleActiveOperator)
    bpy.utils.register_class(CreateRefsOperator)
    bpy.utils.register_class(ToggleRecordOperator)
    bpy.utils.register_class(ConvertSubframesOperator)

    bpy.types.Scene.OVRContext = bpy.props.PointerProperty(type=OVRContext)

    bpy.utils.register_class(PANEL_UL_TrackerList)
    bpy.utils.register_class(RecorderPanel)

    bpy.app.handlers.depsgraph_update_post.clear()
    bpy.app.handlers.depsgraph_update_post.append(scene_update_callback)


def unregister():
    print("Unloading Tracking Toolkit...")

    stop_preview()

    bpy.utils.unregister_class(PANEL_UL_TrackerList)
    bpy.utils.unregister_class(RecorderPanel)

    del bpy.types.Scene.OVRContext

    bpy.utils.unregister_class(ConvertSubframesOperator)
    bpy.utils.unregister_class(ToggleRecordOperator)
    bpy.utils.unregister_class(CreateRefsOperator)
    bpy.utils.unregister_class(ToggleActiveOperator)

    bpy.utils.unregister_class(OVRContext)
    bpy.utils.unregister_class(OVRInput)
    bpy.utils.unregister_class(OVRTracker)
    bpy.utils.unregister_class(OVRTarget)
    bpy.utils.unregister_class(OVRTransform)
    bpy.utils.unregister_class(Preferences)

    bpy.app.handlers.depsgraph_update_post.clear()

    print("Unloaded Tracking Toolkit")


if __name__ == "__main__":
    register()
