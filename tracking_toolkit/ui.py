import bpy
import openvr
from bl_ui.space_view3d_toolbar import View3DPanel

from .operators import (
    ToggleActiveOperator,
    CreateRefsOperator,
    ToggleRecordOperator,
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

        activate_label = "Disconnect/Reset OpenVR" if ovr_context.enabled else "Start/Connect OpenVR"
        layout.operator(ToggleActiveOperator.bl_idname, text=activate_label)

        layout.label(text="Manage Trackers")
        layout.template_list(
            "PANEL_UL_TrackerList",
            "",
            ovr_context,
            "trackers",
            ovr_context,
            "selected_tracker",
            rows=max(len(ovr_context.trackers), 1),
            type="DEFAULT"
        )

        layout.operator(CreateRefsOperator.bl_idname, text="Create References")

        if not ovr_context.enabled:
            return

        layout.label(text="Recording")

        record_btn_row = layout.row()
        record_btn_row.scale_y = 2
        record_btn_row.alert = ovr_context.recording

        active_record_label = "Stop Recording" if ovr_context.recording else "Start Recording"
        active_record_icon = "RECORD_ON" if ovr_context.recording else "RECORD_OFF"

        # noinspection PyTypeChecker
        record_btn_row.operator(
            ToggleRecordOperator.bl_idname,
            text=active_record_label,
            icon=active_record_icon,
            depress=True,
        )

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

            try:
                system = openvr.VRSystem()
                role_prop = openvr.Prop_ControllerRoleHint_Int32
                left_role = getattr(openvr, "TrackedControllerRole_LeftHand", 1)
                right_role = getattr(openvr, "TrackedControllerRole_RightHand", 2)

                for tracker in ovr_context.trackers:
                    tracker_obj = tracker.target.object or bpy.data.objects.get(tracker.name)
                    if tracker.type != controller_type or not tracker_obj:
                        continue
                    try:
                        role = system.getInt32TrackedDeviceProperty(tracker.index, role_prop)
                    except Exception:
                        role = None

                    if role == left_role and not left_obj:
                        left_obj = tracker_obj
                    elif role == right_role and not right_obj:
                        right_obj = tracker_obj
            except Exception:
                pass

            return left_obj, right_obj

        left_controller, right_controller = resolve_controller_objects()

        draw_controller_props(layout, "Left Controller", left_controller)
        draw_controller_props(layout, "Right Controller", right_controller)
