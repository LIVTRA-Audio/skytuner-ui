class SkyTunerError(Exception):
    """Base class for expected updater failures."""


class DeviceNotFoundError(SkyTunerError):
    """No unambiguous SkyTuner USB device was found."""


class ProtocolError(SkyTunerError):
    """A device or host packet violated the SkyTuner protocol."""


class UpdateTimeoutError(SkyTunerError):
    """The device did not respond within the update timeout."""
