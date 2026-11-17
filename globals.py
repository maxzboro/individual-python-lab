from PySide6.QtGui import QIntValidator, QDoubleValidator, QFont

# Global aesthetic configs
INPUT_FIELDS_W = 80
DISCONNECT_LITERAL = "[від'єднати]" # Is used to reduce the amount of buttons on the tool panel
TESTING_LITERAL = "[тестувати]"

# Validators, exported into the global namespace for convenience and mem management
positiveOnly = QIntValidator()
positiveOnly.setBottom(1)
positDoublesOnly = QDoubleValidator()
positDoublesOnly.setBottom(0)
doublesOnly = QDoubleValidator() # Uses commas as float separator

importantFont = QFont()
importantFont.setBold(True)
importantFont.setPointSize(14)

BINARY_FORMATS = {
    'short': 'h',
    'unsigned short': 'H',
    'int': 'i',
    'unsigned int': 'I',
    'long': 'l',
    'unsigned long': 'L',
    'long long': 'q',
    'unsigned long long': 'Q',
    'float': 'f',
    'double': 'd'
}
BINARY_FORMATS_INV = dict(zip(BINARY_FORMATS.values(), BINARY_FORMATS.keys())) # basically the bin format dict but with keys an pairs swapped
ENDIAN_DICT = {
    'Native': '=',
    'Little-Endian': '<',
    'Big-Endian': '>'
}


def process_time_window(twin: float, maxtime: float) -> float:
    if twin is None or abs(twin)>maxtime: # handling invalid time window specs
        return maxtime
    else:
        return twin if twin>=0 else maxtime+twin # negative means subtract from last