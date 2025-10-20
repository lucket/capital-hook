import time

class TrailRecalibration:
    def __init__(self, pnl: float, recalibrate_at: float, trail: float):
        """
        Initialize Trail Recalibration tracker.
        """
        self.pnl = pnl
        self.recalibrate_at = recalibrate_at
        self.trail = trail

        # Internal tracking
        self.new_max = pnl
        self.cutoff = None
        self.last_recalibration_time = 0
        self.cooldown_period = 30  # seconds
        self.is_active = False  # currently in trailing mode
        self.in_recalibration = False  # whether recalibration is still active (cooldown phase)

    def update_pnl(self, pnl: float):
        """
        Update real-time PnL value and manage state transitions.
        Returns:
            bool: True if recalibration condition met or in cooldown, False otherwise.
        """
        self.pnl = pnl

        #If in cooldown, keep returning True until cooldown expires
        if self.in_recalibration:
            if self._cooldown_expired():
                self.in_recalibration = False  # reset
                return False
            return True  # maintain recalibration signal

        # If cooling down prevented recalibration start, skip updating
        if not self._can_recalibrate():
            return False

        # If PnL crosses recalibration threshold, start trailing
        if not self.is_active and pnl >= self.recalibrate_at:
            self._activate_trailing()
            return False

        # While trailing
        if self.is_active:
            if pnl > self.new_max:
                self.new_max = pnl
                self.cutoff = self.new_max - self.trail

            if pnl <= self.cutoff:
                return self.recalibrate()

        return False

    def recalibrate(self) -> bool:
        """
        Trigger recalibration event (when cutoff hit).
        """
        self.last_recalibration_time = time.time()
        self.is_active = False
        self.in_recalibration = True  # signal stays True during cooldown
        self.new_max = self.pnl
        self.cutoff = None
        return True

    def _activate_trailing(self):
        """Activate the trailing logic once threshold is crossed."""
        self.is_active = True
        self.new_max = self.pnl
        self.cutoff = self.new_max - self.trail

    def _can_recalibrate(self) -> bool:
        """Whether we can start a new recalibration."""
        return (time.time() - self.last_recalibration_time) > self.cooldown_period

    def _cooldown_expired(self) -> bool:
        """Whether the cooldown phase has finished."""
        return (time.time() - self.last_recalibration_time) >= self.cooldown_period

    def status(self) -> dict:
        """Snapshot of state for monitoring/logging."""
        cooldown_remaining = max(
            0, self.cooldown_period - (time.time() - self.last_recalibration_time)
        )
        return {
            "pnl": self.pnl,
            "is_active": self.is_active,
            "in_recalibration": self.in_recalibration,
            "new_max": self.new_max,
            "cutoff": self.cutoff,
            "recalibrate_at": self.recalibrate_at,
            "trail": self.trail,
            "cooldown_remaining": cooldown_remaining,
        }
