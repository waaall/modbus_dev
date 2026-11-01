"""配置错误的界面本地化，保留 CLI 的结构化诊断。"""
from ..domain.events import ConfigurationError
from .i18n import tr

TEMPLATES = {
    "must be an integer": tr("Enter an integer."),
    "must be a number": tr("Enter a number."),
    "numeric_range": tr("Enter a finite value from {low} to {high}."),
    "invalid_choice": tr("Choose one of: {choices}."),
    "must be a nonempty string": tr("Enter a non-empty value."),
    "must be an object": tr("An object is required."),
    "unknown_fields": tr("Unknown fields: {fields}."),
    "must be a boolean": tr("A boolean value is required."),
    "must be an array of strings": tr("A list of strings is required."),
    "must be a string": tr("A string is required."),
    "must be positive and below output Nyquist frequency": tr("Frequency must be positive and below the output Nyquist frequency."),
    "signal exceeds finite float32 range": tr("The signal exceeds the finite float32 range."),
    "provide one unique channel per point": tr("Provide one unique channel per point."),
    "config_version": tr("Incompatible configuration version. Only version {current} is supported; recreate the configuration."),
    "integer_source": tr("Integer points support constant and triangle sources only."),
    "triangle_bounds": tr("The upper bound must be greater than the lower bound."),
    "integer_value": tr("Enter an integral value; integer points do not round fractional values."),
    "point_count": tr("Provide 1 to {maximum} point configurations."),
    "readonly_write": tr("A read-only master cannot perform writes."),
    "no_enabled_points": tr("Enable at least one point"),
    "point_overlap": tr("The register ranges of point {first} and point {second} overlap."),
    "transaction_budget": tr("The timeout must exceed the {minimum:.3f} ms transaction budget and leave {gap:.3f} ms of silence before the next cycle."),
    "cycle_minimum": tr("Minimum cycle is {minimum} ms for {requests} requests, including the 10 ms workflow reserve. Increase the period, raise the baud rate or reduce requests."),
    "fc06_width": tr("FC06 writes one 16-bit register. Use FC16 for a 32-bit point."),
    "holding_initial": tr("Holding registers accept an initial constant only; use input registers for continuous source updates."),
    "duplicate_field": tr("Duplicate configuration field: {field}."),
    "invalid_json": tr("Invalid JSON at line {line}, column {column}."),
    "numeric_ip": tr("TCP master requires a numeric IPv4 or IPv6 address."),
    "tcp_cycle_budget": tr("The whole-cycle transaction budget must be smaller than the polling period."),
}

FIELD_NAMES = {
    "serial.port": tr("Serial port"), "serial.baud": tr("Baud rate"), "serial.parity": tr("Parity"),
    "serial.stop_bits": tr("Stop bits"), "unit": tr("Unit ID"), "host": tr("Bind address"), "port": tr("Port"),
    "period_ms": tr("Data period (ms)"), "timeout_ms": tr("I/O timeout (ms)"), "duration_s": tr("Duration (s)"),
    "points": tr("Points"), "schema_version": tr("Configuration version"),
    "logging.max_files": tr("Maximum log segments"),
    "connect_timeout_ms": tr("Connection timeout (ms)"),
}


def format_error(error):
    if isinstance(error, ConfigurationError):
        source = TEMPLATES.get(error.reason)
        if source is not None:
            field = tr("Target IP address") if error.reason == "numeric_ip" else FIELD_NAMES.get(error.path, error.path)
            return tr("{field}: {message}").format(
                field=field, message=source.format(**error.parameters))
    if str(error) == "This build runs constant/sine/triangle sources only; offline sources are deferred until after GUI":
        return tr("This build supports constant, sine and triangle sources only; offline sources are not available.")
    return str(error)
