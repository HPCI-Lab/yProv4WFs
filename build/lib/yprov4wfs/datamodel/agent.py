#------------------AGENT------------------–#
from yprov4wfs.datamodel.node import Node


class Agent:
    """
    Represents an agent (compliant with W3C-PROV) with an ID and name, and maintains relationships
    with other agents it acts for, is attributed to, or is associated with.
    """
    def __init__(self, id: str, name: str):
        self._id: str = id
        self._name: str = name
        self._acted_for: Agent | None = None
        self._attributed_to = []
        self._associated_with = [Node]

    def set_acted_for(self, agent: 'Agent'):
        self._acted_for = agent
    