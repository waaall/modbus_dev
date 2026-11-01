"""主站共用的寄存器请求构造；PyModbus 类型仅在引擎边界内流转。"""
from pymodbus.pdu.register_message import (
    ReadHoldingRegistersRequest, ReadInputRegistersRequest,
    WriteMultipleRegistersRequest, WriteSingleRegisterRequest,
)

_REQUESTS = {3: ReadHoldingRegistersRequest, 4: ReadInputRegistersRequest,
             6: WriteSingleRegisterRequest, 16: WriteMultipleRegistersRequest}


def request_pdu(request, unit):
    return _REQUESTS[request.function](dev_id=unit, address=request.address,
                                      count=request.count, registers=list(request.registers))
