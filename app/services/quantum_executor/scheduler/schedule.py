from typing import List, Union

from quantify_scheduler import Operation

from ..scheduler.channel import Channel


class SimulationSchedule:

    def __init__(self,
                 name: str = ""):
        self._name: str = name
        self._operations: List[BaseOperation] = []

    def add(self,
            operation: 'BaseOperation'):
        # TODO: Here, we chain all operations
        self._operations.append(operation)

    @property
    def operations(self) -> List['BaseOperation']:
        return self._operations

    @operations.setter
    def operations(self, value: List['BaseOperation']):
        self._operations = value

    @property
    def discrete_steps(self):
        # TODO: this function has to be properly tested
        t0_max = 0
        for operation in self.operations:
            if operation.t0 >= t0_max and isinstance(operation, UnitaryOperation):
                t0_max += operation.discrete_steps
        return t0_max


class BaseOperation(Operation):

    def __init__(self,
                 channel: Union[str, int],
                 t0: Union[int, float] = 0):
        super().__init__("")  # Takes name as input
        self.channel = channel
        self.t0 = t0


class UnitaryOperation(BaseOperation):

    def __init__(self,
                 *args,
                 frequency=0.0,
                 phase=0.0,
                 amp=0.0,
                 sigma=1,
                 discrete_steps=0,
                 **kwargs):
        super().__init__(*args, **kwargs)
        self.frequency = frequency,
        self.phase = phase,
        self.amp = amp,
        self.sigma = sigma
        self.discrete_steps = discrete_steps

# TODO: Implement a unitary operation that rotates the qubit or changes the phase


class MeasurementOperation(BaseOperation):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
