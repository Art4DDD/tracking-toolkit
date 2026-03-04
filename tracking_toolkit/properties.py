import bpy

from .. import __package__ as base_package


class OVRTransform(bpy.types.PropertyGroup):
    location: bpy.props.FloatVectorProperty(name="Location", default=(0, 0, 0))
    rotation: bpy.props.FloatVectorProperty(name="Rotation", default=(0, 0, 0))
    scale: bpy.props.FloatVectorProperty(name="Scale", default=(1, 1, 1))


class OVRTarget(bpy.types.PropertyGroup):
    object: bpy.props.PointerProperty(name="Target object", type=bpy.types.Object)
    transform: bpy.props.PointerProperty(type=OVRTransform)


def tracker_name_change_callback(self, _):
    tracker_ref = bpy.data.objects.get(self.prev_name)
    if not tracker_ref:
        return

    tracker_ref.name = self.name
    self.prev_name = self.name


class OVRTracker(bpy.types.PropertyGroup):
    index: bpy.props.IntProperty(name="OpenVR name")
    name: bpy.props.StringProperty(name="Tracker name", update=tracker_name_change_callback)
    prev_name: bpy.props.StringProperty(name="Tracker name before renaming")
    serial: bpy.props.StringProperty(name="Tracker serial string")
    type: bpy.props.StringProperty(name="Tracker type")

    connected: bpy.props.BoolProperty(name="Is tracker connected")

    target: bpy.props.PointerProperty(type=OVRTarget)


class OVRInput(bpy.types.PropertyGroup):
    joystick_position: bpy.props.FloatVectorProperty(name="Joystick position", size=2, default=(0, 0))

    grip_strength: bpy.props.FloatProperty(name="Grip strength", default=0)
    trigger_strength: bpy.props.FloatProperty(name="Trigger strength", default=0)

    # Finger curls (primarily for Valve Index/Knuckles; fallback estimation when unavailable)
    thumb_curl: bpy.props.FloatProperty(name="Thumb curl", default=0, min=0, max=1)
    index_curl: bpy.props.FloatProperty(name="Index curl", default=0, min=0, max=1)
    middle_curl: bpy.props.FloatProperty(name="Middle curl", default=0, min=0, max=1)
    ring_curl: bpy.props.FloatProperty(name="Ring curl", default=0, min=0, max=1)
    pinky_curl: bpy.props.FloatProperty(name="Pinky curl", default=0, min=0, max=1)

    a_button: bpy.props.BoolProperty(name="A pressed", default=False)
    b_button: bpy.props.BoolProperty(name="B pressed", default=False)


def selected_tracker_change_callback(self: "OVRContext", context):
    if self.selected_tracker < 0 or self.selected_tracker >= len(self.trackers):
        return

    selected_tracker = self.trackers[self.selected_tracker]
    obj = selected_tracker.target.object
    if not obj:
        return

    view_layer = getattr(context, "view_layer", None)
    if not view_layer:
        return

    for scene_obj in context.scene.objects:
        scene_obj.select_set(False)

    obj.select_set(True)
    view_layer.objects.active = obj


class OVRContext(bpy.types.PropertyGroup):
    enabled: bpy.props.BoolProperty(name="OpenVR active", default=False)
    recording: bpy.props.BoolProperty(name="OpenVR recording", default=False)
    references_created: bpy.props.BoolProperty(name="References created", default=False)
    recordings_made: bpy.props.BoolProperty(name="Recordings made", default=False)

    trackers: bpy.props.CollectionProperty(type=OVRTracker)
    selected_tracker: bpy.props.IntProperty(name="Selected tracker", default=0, update=selected_tracker_change_callback)

    record_start_frame: bpy.props.IntProperty(name="Recording start frame", default=0)

    l_input: bpy.props.PointerProperty(type=OVRInput, name="Left controller input state")
    r_input: bpy.props.PointerProperty(type=OVRInput, name="Right controller input state")


class Preferences(bpy.types.AddonPreferences):
    bl_idname = base_package

    steamvr_installation_path: bpy.props.StringProperty(
        name="SteamVR Installation Path",
        subtype="FILE_PATH",
        default="C:/Program Files (x86)/Steam/steamapps/common/SteamVR"
    )

    def draw(self, _):
        layout = self.layout
        layout.label(text="Preferences for Tracking Toolkit")
        layout.prop(self, "steamvr_installation_path")
