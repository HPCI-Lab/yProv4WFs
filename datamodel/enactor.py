#------------------ENACTOR------------------–#
class Enactor:
    def __init__(self, id: str, name: str):
        self._id = id
        self._name = name
        self._acted_for = None
        self._attributed_to = []
        self._associated_with = []

    def set_acted_for(self, enactor: 'Enactor'):
        self._acted_for = enactor
    