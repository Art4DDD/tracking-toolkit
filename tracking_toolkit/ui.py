import bpy
import openvr
from bl_ui.space_view3d_toolbar import View3DPanel

from .operators import (
    ToggleActiveOperator,
    ToggleCalibrationOperator,
    CreateRefsOperator,
    ToggleRecordOperator,
    BuildArmatureOperator
)
from .properties import OVRContext


class PANEL_UL_TrackerList(bpy.types.UIList):
    def draw_item(
            self,
            context,
            layout,
            data,
            item,
            icon,
            active_data,
            active_property,
            index,
            flt_flag,
    ):
        selected_tracker = item
        layout.prop(selected_tracker, "name", text="", emboss=False, icon_value=icon)


class RecorderPanel(View3DPanel, bpy.types.Panel):
    bl_idname = "VIEW3D_PT_openvr_recorder_menu"
    bl_label = "Tracking Toolkit Recorder"
    bl_category = "Track TK"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"

    def draw(self, context: bpy.types.Context):
        layout = self.layout
        ovr_context: OVRContext = context.scene.OVRContext

        layout.label(text="Tracking Toolkit Recorder")

        # Toggle active button
        # It's super annoying to have Blender not save the state of this button on save, so we just label it funny
        activate_label = "Disconnect/Reset OpenVR" if ovr_context.enabled else "Start/Connect OpenVR"
        layout.operator(ToggleActiveOperator.bl_idname, text=activate_label)

        # Trackers
        layout.label(text="Manage Trackers")

        # Default armature
        layout.prop(ovr_context, "armature", placeholder="Default Armature")

        # Tracker management
        layout.template_list(
            "PANEL_UL_TrackerList",
            "",
            ovr_context,
            "trackers",
            ovr_context,
            "selected_tracker",
            rows=len(ovr_context.trackers),
            type="DEFAULT"
        )

        # Bone binding
        layout.label(text="Bone binding")
        layout.label(text="Note: you may want to use the Armature Tools panel instead")

        if ovr_context.selected_tracker and ovr_context.selected_tracker < len(ovr_context.trackers):
            selected_tracker = ovr_context.trackers[ovr_context.selected_tracker]

            layout.prop(selected_tracker, "armature", placeholder="Override Armature")
            layout.prop(selected_tracker, "bone", placeholder="Bound Bone")

        # Create empties
        layout.operator(CreateRefsOperator.bl_idname, text="Create References")

        # Show the rest if OpenVR is running
        if not ovr_context.enabled:
            return

        # Calibration
        layout.label(text="Calibration:")

        # Toggle calibration button
        if ovr_context.calibration_stage == 1:
            calibrate_btn_label = "Continue to Offset"
            calibrate_hint = "Stage 1: Line up the opaque tracker models with the character"
        elif ovr_context.calibration_stage == 2:
            calibrate_btn_label = "Complete Calibration"
            calibrate_hint = "Stage 2: Offset the wireframe tracker models to correct the pose"
        else:
            calibrate_btn_label = "Start Calibration"
            calibrate_hint = "Calibration complete"

        layout.operator(ToggleCalibrationOperator.bl_idname, text=calibrate_btn_label)
        layout.label(text=calibrate_hint)

        # Recording
        layout.label(text="Recording")

        # Make button big
        record_btn_row = layout.row()
        record_btn_row.scale_y = 2
        record_btn_row.alert = ovr_context.recording

        start_record_label = "Start Recording"
        stop_record_label = "Stop Recording"
        active_record_label = stop_record_label if ovr_context.recording else start_record_label

        start_record_icon = "RECORD_OFF"
        stop_record_icon = "RECORD_ON"
        active_record_icon = stop_record_icon if ovr_context.recording else start_record_icon

        # I hate warnings (for icon type checking)
        # noinspection PyTypeChecker
        record_btn_row.operator(
            ToggleRecordOperator.bl_idname,
            text=active_record_label,
            icon=active_record_icon,
            depress=True,
        )

        # Skeletal finger debug (Knuckles)
        layout.label(text="Skeletal Fingers")

        def draw_controller_props(block_layout, title: str, controller_obj: bpy.types.Object | None):
            box = block_layout.box()
            box.label(text=title)
            if not controller_obj:
                box.label(text="Controller reference not found", icon="ERROR")
                return

            box.label(text=f"Object: {controller_obj.name}")
            for channel, label in (
                ("thumb_curl", "Thumb"),
                ("index_curl", "Index"),
                ("middle_curl", "Middle"),
                ("ring_curl", "Ring"),
                ("pinky_curl", "Pinky"),
            ):
                if channel not in controller_obj:
                    controller_obj[channel] = 0.0
                box.prop(controller_obj, f'["{channel}"]', text=label)

        def resolve_controller_objects() -> tuple[bpy.types.Object | None, bpy.types.Object | None]:
            left_obj = None
            right_obj = None
            controller_type = str(openvr.TrackedDeviceClass_Controller)

            def is_virtual_lhr_name(name: str) -> bool:
                return (name or "").upper().startswith("LHR-FFFFFF")

            def find_existing(hand: str) -> bpy.types.Object | None:
                hand = hand.lower()
                candidates = {
                    "left": ["left controller", "controller left", "controller_l", "controller.l", "left_hand", "l_hand", "index left", "knuckles left"],
                    "right": ["right controller", "controller right", "controller_r", "controller.r", "right_hand", "r_hand", "index right", "knuckles right"],
                }
                for obj in bpy.data.objects:
                    name = obj.name.lower()
                    if any(token in name for token in candidates.get(hand, [])):
                        return obj
                return None

            left_obj = find_existing("left")
            right_obj = find_existing("right")

            try:
                system = openvr.VRSystem()
                role_prop = openvr.Prop_ControllerRoleHint_Int32
                left_role = getattr(openvr, "TrackedControllerRole_LeftHand", 1)
                right_role = getattr(openvr, "TrackedControllerRole_RightHand", 2)

                role_candidates = {"left": [], "right": []}

                for tracker in ovr_context.trackers:
                    tracker_obj = tracker.target.object or bpy.data.objects.get(tracker.name)
                    if tracker.type != controller_type or not tracker_obj:
                        continue
                    try:
                        role = system.getInt32TrackedDeviceProperty(tracker.index, role_prop)
                    except Exception:
                        role = None

                    if role == left_role:
                        role_candidates["left"].append((tracker.name, tracker_obj))
                    elif role == right_role:
                        role_candidates["right"].append((tracker.name, tracker_obj))

                if not left_obj and role_candidates["left"]:
                    non_virtual = [obj for name, obj in role_candidates["left"] if not is_virtual_lhr_name(name)]
                    left_obj = non_virtual[0] if non_virtual else role_candidates["left"][0][1]

                if not right_obj and role_candidates["right"]:
                    non_virtual = [obj for name, obj in role_candidates["right"] if not is_virtual_lhr_name(name)]
                    right_obj = non_virtual[0] if non_virtual else role_candidates["right"][0][1]
            except Exception:
                pass

            if not left_obj:
                left_obj = next(((t.target.object or bpy.data.objects.get(t.name)) for t in ovr_context.trackers if t.type == controller_type and "left" in t.name.lower()), None)
            if not right_obj:
                right_obj = next(((t.target.object or bpy.data.objects.get(t.name)) for t in ovr_context.trackers if t.type == controller_type and "right" in t.name.lower()), None)

            return left_obj, right_obj

        left_controller, right_controller = resolve_controller_objects()

        draw_controller_props(layout, "Left Controller", left_controller)
        draw_controller_props(layout, "Right Controller", right_controller)


class ArmaturePanel(View3DPanel, bpy.types.Panel):
    bl_idname = "VIEW3D_PT_openvr_armature_menu"
    bl_label = "Armature Tools"
    bl_category = "Track TK"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"

    def draw(self, context: bpy.types.Context):
        layout = self.layout
        ovr_context: OVRContext = context.scene.OVRContext

        joints = ovr_context.armature_joints

        layout.prop(joints, "head")
        layout.prop(joints, "chest")
        layout.prop(joints, "hips")

        layout.prop(joints, "r_hand")
        layout.prop(joints, "l_hand")

        layout.prop(joints, "r_elbow")
        layout.prop(joints, "l_elbow")

        layout.prop(joints, "r_foot")
        layout.prop(joints, "l_foot")

        layout.prop(joints, "r_knee")
        layout.prop(joints, "l_knee")

        layout.operator(BuildArmatureOperator.bl_idname)
