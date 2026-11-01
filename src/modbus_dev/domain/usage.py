"""角色和操作决定参数是否参与配置校验、界面编辑及运行。"""
from dataclasses import dataclass, field
import os


@dataclass(frozen=True)
class ParameterUsage:
    mode: str
    operation: str = "write-multiple"
    readback_every: int = 0
    rts_supported: bool = field(default_factory=lambda: os.name == "nt")

    @property
    def master(self):
        return self.mode in ("rtu-master", "tcp-master")

    @property
    def tcp(self):
        return self.mode in ("tcp-master", "tcp-slave")

    @property
    def serial(self):
        return self.mode in ("rtu-master", "rtu-slave")

    @property
    def writing(self):
        return self.master and self.operation in ("write-single", "write-multiple")

    @property
    def source(self):
        return not self.master or self.writing

    @property
    def readback(self):
        return self.writing and bool(self.readback_every)

    def field_active(self, key):
        if key == "serial.rts_toggle":
            return self.serial and self.rts_supported
        if key == "access_mode":
            return True
        if key.startswith("serial."):
            return self.serial
        if key in ("host", "port"):
            return self.tcp
        if key == "connect_timeout_ms":
            return self.mode == "tcp-master"
        if key == "schedule_tolerance_percent":
            return self.master
        if key == "readback_every":
            return self.writing
        return True
