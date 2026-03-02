#!/usr/bin/env python3
from __future__ import annotations
import argparse, time
from dataclasses import dataclass
import openvr

@dataclass
class DeviceSnapshot:
    index:int; device_class:int; serial:str; model:str; render_model:str; registered_type:str; controller_type:str; input_profile:str; connected:bool

def _safe_str(system, index:int, prop:int)->str:
    try: return system.getStringTrackedDeviceProperty(index, prop).strip()
    except Exception: return ""

def read_snapshot(system, index:int)->DeviceSnapshot:
    return DeviceSnapshot(index=index, device_class=system.getTrackedDeviceClass(index), serial=_safe_str(system,index,openvr.Prop_SerialNumber_String), model=_safe_str(system,index,openvr.Prop_ModelNumber_String), render_model=_safe_str(system,index,openvr.Prop_RenderModelName_String), registered_type=_safe_str(system,index,getattr(openvr,"Prop_RegisteredDeviceType_String",-1)), controller_type=_safe_str(system,index,getattr(openvr,"Prop_ControllerType_String",-1)), input_profile=_safe_str(system,index,getattr(openvr,"Prop_InputProfilePath_String",-1)), connected=bool(system.isTrackedDeviceConnected(index)))

def fmt(s:DeviceSnapshot)->str:
    return f"idx={s.index} class={s.device_class} connected={s.connected} serial={s.serial or '-'} model={s.model or '-'} render={s.render_model or '-'} registered={s.registered_type or '-'} controller={s.controller_type or '-'} input={s.input_profile or '-'}"

def main()->int:
    ap=argparse.ArgumentParser(); ap.add_argument('--duration',type=float,default=120.0); ap.add_argument('--interval',type=float,default=0.5); a=ap.parse_args()
    openvr.init(openvr.VRApplication_Background)
    try:
        system=openvr.VRSystem(); prev:dict[int,DeviceSnapshot]={}; end=time.time()+a.duration
        while time.time()<end:
            for idx in range(openvr.k_unMaxTrackedDeviceCount):
                if system.getTrackedDeviceClass(idx)==openvr.TrackedDeviceClass_Invalid: continue
                snap=read_snapshot(system,idx); old=prev.get(idx)
                if old is None: print('[NEW]',fmt(snap))
                elif old.serial!=snap.serial or old.connected!=snap.connected or old.render_model!=snap.render_model or old.input_profile!=snap.input_profile:
                    print('[CHG]',fmt(old)); print('   ->',fmt(snap))
                prev[idx]=snap
            time.sleep(a.interval)
    finally:
        openvr.shutdown()
    return 0

if __name__=='__main__': raise SystemExit(main())
