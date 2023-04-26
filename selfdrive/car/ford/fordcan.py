from cereal import car
from selfdrive.car.ford.values import CANBUS

HUDControl = car.CarControl.HUDControl


def calculate_lat_ctl2_checksum(mode: int, counter: int, dat: bytearray):
  checksum = mode + counter
  checksum += dat[2] + ((dat[3] & 0xE0) >> 5)           # curvature
  checksum += dat[6] + ((dat[7] & 0xE0) >> 5)           # curvature rate
  checksum += (dat[3] & 0x1F) + ((dat[4] & 0xFC) >> 2)  # path angle
  checksum += (dat[4] & 0x3) + dat[5]                   # path offset
  return 0xFF - (checksum & 0xFF)


def create_lka_msg(packer):
  """
  Creates an empty CAN message for the Ford LKA Command.

  This command can apply "Lane Keeping Aid" manoeuvres, which are subject to the PSCM lockout.

  Frequency is 20Hz.
  """

  return packer.make_can_msg("Lane_Assist_Data1", CANBUS.main, {})


def create_lat_ctl_msg(packer, lat_active: bool, path_offset: float, path_angle: float, curvature: float,
                       curvature_rate: float):
  """
  Creates a CAN message for the Ford TJA/LCA Command.

  This command can apply "Lane Centering" manoeuvres: continuous lane centering for traffic jam assist and highway
  driving. It is not subject to the PSCM lockout.

  Ford lane centering command uses a third order polynomial to describe the road centerline. The polynomial is defined
  by the following coefficients:
    c0: lateral offset between the vehicle and the centerline (positive is right)
    c1: heading angle between the vehicle and the centerline (positive is right)
    c2: curvature of the centerline (positive is left)
    c3: rate of change of curvature of the centerline
  As the PSCM combines this information with other sensor data, such as the vehicle's yaw rate and speed, the steering
  angle cannot be easily controlled.

  The PSCM should be configured to accept TJA/LCA commands before these commands will be processed. This can be done
  using tools such as Forscan.

  Frequency is 20Hz.
  """

  values = {
    "LatCtlRng_L_Max": 0,                       # Unknown [0|126] meter
    "HandsOffCnfm_B_Rq": 0,                     # Unknown: 0=Inactive, 1=Active [0|1]
    "LatCtl_D_Rq": 1 if lat_active else 0,      # Mode: 0=None, 1=ContinuousPathFollowing, 2=InterventionLeft,
                                                #       3=InterventionRight, 4-7=NotUsed [0|7]
    "LatCtlRampType_D_Rq": 0,                   # Ramp speed: 0=Slow, 1=Medium, 2=Fast, 3=Immediate [0|3]
                                                #             Makes no difference with curvature control
    "LatCtlPrecision_D_Rq": 1,                  # Precision: 0=Comfortable, 1=Precise, 2/3=NotUsed [0|3]
                                                #            The stock system always uses comfortable
    "LatCtlPathOffst_L_Actl": path_offset,      # Path offset [-5.12|5.11] meter
    "LatCtlPath_An_Actl": path_angle,           # Path angle [-0.5|0.5235] radians
    "LatCtlCurv_NoRate_Actl": curvature_rate,   # Curvature rate [-0.001024|0.00102375] 1/meter^2
    "LatCtlCurv_No_Actl": curvature,            # Curvature [-0.02|0.02094] 1/meter
  }
  return packer.make_can_msg("LateralMotionControl", CANBUS.main, values)


def create_lat_ctl2_msg(packer, mode: int, path_offset: float, path_angle: float, curvature: float,
                        curvature_rate: float, counter: int):
  """
  Create a CAN message for the new Ford Lane Centering command.

  This message is used on the CAN FD platform and replaces the old LateralMotionControl message. It is similar but has
  additional signals for a counter and checksum.

  Frequency is 20Hz.
  """

  values = {
    "LatCtl_D2_Rq": mode,                       # Mode: 0=None, 1=PathFollowingLimitedMode, 2=PathFollowingExtendedMode,
                                                #       3=SafeRampOut, 4-7=NotUsed [0|7]
    "LatCtlRampType_D_Rq": 0,                   # 0=Slow, 1=Medium, 2=Fast, 3=Immediate [0|3]
    "LatCtlPrecision_D_Rq": 1,                  # 0=Comfortable, 1=Precise, 2/3=NotUsed [0|3]
    "LatCtlPathOffst_L_Actl": path_offset,      # [-5.12|5.11] meter
    "LatCtlPath_An_Actl": path_angle,           # [-0.5|0.5235] radians
    "LatCtlCurv_No_Actl": curvature,            # [-0.02|0.02094] 1/meter
    "LatCtlCrv_NoRate2_Actl": curvature_rate,   # [-0.001024|0.001023] 1/meter^2
    "HandsOffCnfm_B_Rq": 0,                     # 0=Inactive, 1=Active [0|1]
    "LatCtlPath_No_Cnt": counter,               # [0|15]
    "LatCtlPath_No_Cs": 0,                      # [0|255]
  }

  # calculate checksum
  dat = packer.make_can_msg("LateralMotionControl2", CANBUS.main, values)[2]
  values["LatCtlPath_No_Cs"] = calculate_lat_ctl2_checksum(mode, counter, dat)

  return packer.make_can_msg("LateralMotionControl2", CANBUS.main, values)


def create_acc_msg(packer, long_active: bool, gas: float, accel: float, precharge_brake: bool, decel: bool):
  """
  Creates a CAN message for the Ford ACC Command.

  This command can be used to enable ACC, to set the ACC gas/brake/decel values
  and to disable ACC.

  Frequency is 50Hz.
  """

  values = {
    "AccBrkTot_A_Rq": accel,                          # Brake total accel request: [-20|11.9449] m/s^2
    "Cmbb_B_Enbl": 1 if long_active else 0,           # Enabled: 0=No, 1=Yes
    "AccPrpl_A_Rq": gas,                              # Acceleration request: [-5|5.23] m/s^2
    "AccBrkPrchg_B_Rq": 1 if precharge_brake else 0,  # Pre-charge brake request: 0=No, 1=Yes
    "AccBrkDecel_B_Rq": 1 if decel else 0,            # Deceleration request: 0=Inactive, 1=Active
  }
  return packer.make_can_msg("ACCDATA", CANBUS.main, values)


def create_acc_ui_msg(packer, main_on: bool, enabled: bool, hud_control, stock_values: dict):
  """
  Creates a CAN message for the Ford IPC adaptive cruise, forward collision warning and traffic jam assist status.

  Stock functionality is maintained by passing through unmodified signals.

  Frequency is 20Hz.
  """

  # Tja_D_Stat
  if enabled:
    if hud_control.leftLaneDepart:
      status = 3  # ActiveInterventionLeft
    elif hud_control.rightLaneDepart:
      status = 4  # ActiveInterventionRight
    else:
      status = 2  # Active
  elif main_on:
    if hud_control.leftLaneDepart:
      status = 5  # ActiveWarningLeft
    elif hud_control.rightLaneDepart:
      status = 6  # ActiveWarningRight
    else:
      status = 1  # Standby
  else:
    status = 0    # Off

  values = {s: stock_values[s] for s in [
    "HaDsply_No_Cs",
    "HaDsply_No_Cnt",
    "AccStopStat_D_Dsply",       # ACC stopped status message
    "AccTrgDist2_D_Dsply",       # ACC target distance
    "AccStopRes_B_Dsply",
    "TjaWarn_D_Rq",              # TJA warning
    "Tja_D_Stat",                # TJA status
    "TjaMsgTxt_D_Dsply",         # TJA text
    "IaccLamp_D_Rq",             # iACC status icon
    "AccMsgTxt_D2_Rq",           # ACC text
    "FcwDeny_B_Dsply",           # FCW disabled
    "FcwMemStat_B_Actl",         # FCW enabled setting
    "AccTGap_B_Dsply",           # ACC time gap display setting
    "CadsAlignIncplt_B_Actl",
    "AccFllwMde_B_Dsply",        # ACC follow mode display setting
    "CadsRadrBlck_B_Actl",
    "CmbbPostEvnt_B_Dsply",      # AEB event status
    "AccStopMde_B_Dsply",        # ACC stop mode display setting
    "FcwMemSens_D_Actl",         # FCW sensitivity setting
    "FcwMsgTxt_D_Rq",            # FCW text
    "AccWarn_D_Dsply",           # ACC warning
    "FcwVisblWarn_B_Rq",         # FCW visible alert
    "FcwAudioWarn_B_Rq",         # FCW audio alert
    "AccTGap_D_Dsply",           # ACC time gap
    "AccMemEnbl_B_RqDrv",        # ACC adaptive/normal setting
    "FdaMem_B_Stat",             # FDA enabled setting
  ]}

  values.update({
    "Tja_D_Stat": status,
  })
  return packer.make_can_msg("ACCDATA_3", CANBUS.main, values)


def create_lkas_ui_msg(packer, main_on: bool, enabled: bool, steer_alert: bool, hud_control, stock_values: dict):
  """
  Creates a CAN message for the Ford IPC IPMA/LKAS status.

  Show the LKAS status with the "driver assist" lines in the IPC.

  Stock functionality is maintained by passing through unmodified signals.

  Frequency is 1Hz.
  """

  # LaActvStats_D_Dsply
  #    R  Intvn Warn Supprs Avail No
  # L
  # Intvn  24    19    14     9   4
  # Warn   23    18    13     8   3
  # Supprs 22    17    12     7   2
  # Avail  21    16    11     6   1
  # No     20    15    10     5   0
  #
  # TODO: test suppress state
  if enabled:
    lines = 0  # NoLeft_NoRight
    if hud_control.leftLaneDepart:
      lines += 4
    elif hud_control.leftLaneVisible:
      lines += 1
    if hud_control.rightLaneDepart:
      lines += 20
    elif hud_control.rightLaneVisible:
      lines += 5
  elif main_on:
    lines = 0
  else:
    if hud_control.leftLaneDepart:
      lines = 3  # WarnLeft_NoRight
    elif hud_control.rightLaneDepart:
      lines = 15  # NoLeft_WarnRight
    else:
      lines = 30  # LA_Off

  hands_on_wheel_dsply = 1 if steer_alert else 0

  values = {s: stock_values[s] for s in [
    "FeatConfigIpmaActl",
    "FeatNoIpmaActl",
    "PersIndexIpma_D_Actl",
    "AhbcRampingV_D_Rq",     # AHB ramping
    "LaActvStats_D_Dsply",   # LKAS status (lines)
    "LaDenyStats_B_Dsply",   # LKAS error
    "LaHandsOff_D_Dsply",    # LKAS hands on chime
    "CamraDefog_B_Req",      # Windshield heater?
    "CamraStats_D_Dsply",    # Camera status
    "DasAlrtLvl_D_Dsply",    # DAS alert level
    "DasStats_D_Dsply",      # DAS status
    "DasWarn_D_Dsply",       # DAS warning
    "AhbHiBeam_D_Rq",        # AHB status
    "Passthru_63",
    "Passthru_48",
  ]}

  values.update({
    "LaActvStats_D_Dsply": lines,                 # LKAS status (lines) [0|31]
    "LaHandsOff_D_Dsply": hands_on_wheel_dsply,   # 0=HandsOn, 1=Level1 (w/o chime), 2=Level2 (w/ chime), 3=Suppressed
  })
  return packer.make_can_msg("IPMA_Data", CANBUS.main, values)


def create_button_msg(packer, stock_values: dict, cancel=False, resume=False, tja_toggle=False,
                      bus: int = CANBUS.camera):
  """
  Creates a CAN message for the Ford SCCM buttons/switches.

  Includes cruise control buttons, turn lights and more.
  """

  values = {s: stock_values[s] for s in [
    "TurnLghtSwtch_D_Stat",    # SCCM Turn signal switch
    "TjaButtnOnOffPress",      # SCCM ACC button, lane-centering/traffic jam assist toggle
    "HeadLghtHiFlash_D_Stat",  # SCCM Passthrough the remaining buttons
    "WiprFront_D_Stat",
    "LghtAmb_D_Sns",
    "AccButtnGapDecPress",
    "AccButtnGapIncPress",
    "AslButtnOnOffCnclPress",
    "AslButtnOnOffPress",
    "CcAslButtnCnclPress",
    "LaSwtchPos_D_Stat",
    "CcAslButtnCnclResPress",
    "CcAslButtnDeny_B_Actl",
    "CcAslButtnIndxDecPress",
    "CcAslButtnIndxIncPress",
    "CcAslButtnOffCnclPress",
    "CcAslButtnOnOffCncl",
    "CcAslButtnOnPress",
    "CcAslButtnResDecPress",
    "CcAslButtnResIncPress",
    "CcAslButtnSetDecPress",
    "CcAslButtnSetIncPress",
    "CcAslButtnSetPress",
    "CcAsllButtnResPress",
    "CcButtnOffPress",
    "CcButtnOnOffCnclPress",
    "CcButtnOnOffPress",
    "CcButtnOnPress",
    "HeadLghtHiFlash_D_Actl",
    "HeadLghtHiOn_B_StatAhb",
    "AhbStat_B_Dsply",
    "AccButtnGapTogglePress",
    "WiprFrontSwtch_D_Stat",
    "HeadLghtHiCtrl_D_RqAhb",
  ]}

  values.update({
    "CcAslButtnCnclPress": 1 if cancel else 0,      # CC cancel button
    "CcAsllButtnResPress": 1 if resume else 0,      # CC resume button
    "TjaButtnOnOffPress": 1 if tja_toggle else 0,   # TJA toggle button
  })
  return packer.make_can_msg("Steering_Data_FD1", bus, values)
