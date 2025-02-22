import math

from cereal import car
from common.conversions import Conversions as CV
from common.numpy_fast import clip, interp
from common.realtime import DT_MDL
from selfdrive.modeld.constants import T_IDXS

# WARNING: this value was determined based on the model's training distribution,
#          model predictions above this speed can be unpredictable
V_CRUISE_MAX = 145  # kph
V_CRUISE_MIN = 30  # Chrysler min ACC when metric
V_CRUISE_DELTA = 5  # ACC increments (unit agnostic)
V_CRUISE_MIN_IMPERIAL = int(20 * CV.MPH_TO_KPH)
V_CRUISE_DELTA_IMPERIAL = int(V_CRUISE_DELTA * CV.MPH_TO_KPH)
V_CRUISE_ENABLE_MIN = 40  # kph
V_CRUISE_INITIAL = 255  # kph

MIN_SPEED = 1.0
LAT_MPC_N = 16
LON_MPC_N = 32
CONTROL_N = 17
CAR_ROTATION_RADIUS = 0.0

# EU guidelines
MAX_LATERAL_JERK = 5.0

ButtonEvent = car.CarState.ButtonEvent
ButtonType = car.CarState.ButtonEvent.Type
CRUISE_LONG_PRESS = 50
CRUISE_NEAREST_FUNC = {
  ButtonType.accelCruise: math.ceil,
  ButtonType.decelCruise: math.floor,
}
CRUISE_INTERVAL_SIGN = {
  ButtonType.accelCruise: +1,
  ButtonType.decelCruise: -1,
}


class VCruiseHelper:
  def __init__(self, CP):
    self.CP = CP
    self.v_cruise_kph = V_CRUISE_INITIAL
    self.v_cruise_cluster_kph = V_CRUISE_INITIAL
    self.v_cruise_kph_last = 0
    self.button_timers = {ButtonType.decelCruise: 0, ButtonType.accelCruise: 0}
    self.button_change_states = {btn: {"standstill": False} for btn in self.button_timers}

  @property
  def v_cruise_initialized(self):
    return self.v_cruise_kph != V_CRUISE_INITIAL

  def update_v_cruise(self, CS, enabled, is_metric, reverse_acc_button_change):
    self.v_cruise_kph_last = self.v_cruise_kph

    if CS.cruiseState.available:
      if not self.CP.pcmCruise or not self.CP.pcmCruiseSpeed:
        # if stock cruise is completely disabled, then we can use our own set speed logic
        self._update_v_cruise_non_pcm(CS, enabled, is_metric, reverse_acc_button_change)
        self.v_cruise_cluster_kph = self.v_cruise_kph
        self.update_button_timers(CS)
      else:
        self.v_cruise_kph = CS.cruiseState.speed * CV.MS_TO_KPH
        self.v_cruise_cluster_kph = CS.cruiseState.speedCluster * CV.MS_TO_KPH
    else:
      self.v_cruise_kph = V_CRUISE_INITIAL
      self.v_cruise_cluster_kph = V_CRUISE_INITIAL

  def _update_v_cruise_non_pcm(self, CS, enabled, is_metric, reverse_acc_button_change):
    v_cruise_min = cruise_min(is_metric)
    if enabled:
      for b in CS.buttonEvents:
        short_press = not b.pressed and b.pressedFrames < 30
        long_press = b.pressed and b.pressedFrames == 30 \
                     or ((not reverse_acc_button_change) and b.pressedFrames % 50 == 0 and b.pressedFrames > 50)

        if reverse_acc_button_change:
          sp = short_press
          short_press = long_press
          long_press = sp

        if long_press:
          v_cruise_delta_5 = V_CRUISE_DELTA if is_metric else V_CRUISE_DELTA_IMPERIAL
          if b.type == car.CarState.ButtonEvent.Type.accelCruise:
            self.v_cruise_kph += v_cruise_delta_5 - (self.v_cruise_kph % v_cruise_delta_5)
          elif b.type == car.CarState.ButtonEvent.Type.decelCruise:
            self.v_cruise_kph -= v_cruise_delta_5 - ((v_cruise_delta_5 - self.v_cruise_kph) % v_cruise_delta_5)
          self.v_cruise_kph = clip(self.v_cruise_kph, v_cruise_min, V_CRUISE_MAX)
        elif short_press:
          v_cruise_delta_1 = 1 if is_metric else CV.MPH_TO_KPH
          if b.type == car.CarState.ButtonEvent.Type.accelCruise:
            self.v_cruise_kph += v_cruise_delta_1
          elif b.type == car.CarState.ButtonEvent.Type.decelCruise:
            self.v_cruise_kph -= v_cruise_delta_1

    return max(self.v_cruise_kph, v_cruise_min)

  def update_button_timers(self, CS):
    # increment timer for buttons still pressed
    for k in self.button_timers:
      if self.button_timers[k] > 0:
        self.button_timers[k] += 1

    for b in CS.buttonEvents:
      if b.type.raw in self.button_timers:
        self.button_timers[b.type.raw] = 1 if b.pressed else 0

  def initialize_v_cruise(self, CS, experimental_mode: bool, is_metric):
    # 250kph or above probably means we never had a set speed
    speed = None
    if self.v_cruise_kph_last < 250:
      for b in CS.buttonEvents:
        if b.type == "resumeCruise":
          speed = self.v_cruise_kph_last

    self.v_cruise_kph = int(round(clip(CS.vEgo * CV.MS_TO_KPH, cruise_min(is_metric), V_CRUISE_MAX))) if speed is None else speed
    self.v_cruise_cluster_kph = self.v_cruise_kph


def cruise_min(is_metric):
  return V_CRUISE_MIN if is_metric else V_CRUISE_MIN_IMPERIAL


def apply_deadzone(error, deadzone):
  if error > deadzone:
    error -= deadzone
  elif error < - deadzone:
    error += deadzone
  else:
    error = 0.
  return error


def apply_center_deadzone(error, deadzone):
  if (error > - deadzone) and (error < deadzone):
    error = 0.
  return error


def rate_limit(new_value, last_value, dw_step, up_step):
  return clip(new_value, last_value + dw_step, last_value + up_step)


def get_lag_adjusted_curvature(CP, v_ego, psis, curvatures, curvature_rates):
  if len(psis) != CONTROL_N:
    psis = [0.0]*CONTROL_N
    curvatures = [0.0]*CONTROL_N
    curvature_rates = [0.0]*CONTROL_N
  v_ego = max(MIN_SPEED, v_ego)

  # TODO this needs more thought, use .2s extra for now to estimate other delays
  delay = CP.steerActuatorDelay + .2

  # MPC can plan to turn the wheel and turn back before t_delay. This means
  # in high delay cases some corrections never even get commanded. So just use
  # psi to calculate a simple linearization of desired curvature
  current_curvature_desired = curvatures[0]
  psi = interp(delay, T_IDXS[:CONTROL_N], psis)
  average_curvature_desired = psi / (v_ego * delay)
  desired_curvature = 2 * average_curvature_desired - current_curvature_desired

  # This is the "desired rate of the setpoint" not an actual desired rate
  desired_curvature_rate = curvature_rates[0]
  max_curvature_rate = MAX_LATERAL_JERK / (v_ego**2) # inexact calculation, check https://github.com/commaai/openpilot/pull/24755
  safe_desired_curvature_rate = clip(desired_curvature_rate,
                                     -max_curvature_rate,
                                     max_curvature_rate)
  safe_desired_curvature = clip(desired_curvature,
                                current_curvature_desired - max_curvature_rate * DT_MDL,
                                current_curvature_desired + max_curvature_rate * DT_MDL)

  return safe_desired_curvature, safe_desired_curvature_rate
