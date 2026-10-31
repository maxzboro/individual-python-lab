import PySide6.QtWidgets as pswidg
from PySide6.QtGui import Qt, QIntValidator, QDoubleValidator
from PySide6.QtCore import QTimer, QRunnable, Slot, QThreadPool
import serial
from serial.tools.list_ports import comports
from time import sleep
import queue

# Global aesthetic configs
INPUT_FIELDS_W = 80
DISCONNECT_LITERAL = "[від'єднати]" # Is used to reduce the amount of buttons on the tool panel

class MainWindow(pswidg.QMainWindow):
    def __init__(self) -> None:
        super().__init__()

        # Serial port instance and control
        self.serial = serial.Serial()
        self.commandQueue = queue.Queue() # to prevent race conditions
        # UARTWorker commands (the $ prefix is used because there are no dollar signs in COM names)
        # ---
        # $CLOSE - close the serial connection;
        # $QUIT - the application is being closed, time to leave;
        # anything else - tries to connect to that port with the given parameters.

        self.setWindowTitle("Multichannel Experimental Data Reader")
        primaryContainer = pswidg.QWidget()
        primaryLayout = pswidg.QVBoxLayout(primaryContainer)
        primaryLayout.setAlignment(Qt.AlignmentFlag.AlignTop)

        # Input field validator for positive ints, exported into the object namespace because it is used ubiquitously
        self.positiveOnly = QIntValidator()
        self.positiveOnly.setBottom(1)

        # Controls bar
        controlsContainer = pswidg.QWidget()
        controlsLayout = pswidg.QHBoxLayout(controlsContainer)

        # UART Port selector
        portLabel = pswidg.QLabel("Порт:")
        self.portCBox = pswidg.QComboBox()
        self.portCBox.addItem(DISCONNECT_LITERAL)
        self.portCBox.currentTextChanged.connect(self.setUART)

        # Baudrate
        baudrateLabel = pswidg.QLabel("Швидкість передачі:")
        self.baudrateCBox = pswidg.QComboBox()
        self.baudrateCBox.addItems(
            ('50', '75', '110', '134', '150', '200', '300', '600', '1200', '1800', \
            '2400', '4800', '9600', '19200', '28800', '38400', '57600', '76800', \
            '115200', '230400', '460800', '576000', '921600') # standard baudrates
        )
        self.baudrateCBox.setCurrentText('9600') # this is by far the most standard baudrate along with 115200
        self.baudrateCBox.setEditable(True)
        self.baudrateCBox.setValidator(self.positiveOnly)

        # Data bits
        NBitsLabel = pswidg.QLabel("Бітів даних:")
        self.NBitsCBox = pswidg.QComboBox()
        self.NBitsCBox.addItems(('8','7','6','5')) # PySerial allows only these

        # Parity
        parityLabel = pswidg.QLabel("Парність:")
        self.parityCBox = pswidg.QComboBox()
        self.parityCBox.addItems(('None', 'Even', 'Odd', 'Mark', 'Space'))

        # Stop bits
        stopLabel = pswidg.QLabel("Стоп-бітів:")
        self.stopCBox = pswidg.QComboBox()
        self.stopCBox.addItems(('1', '2', '1.5'))

        # Channel amount
        nChanLabel = pswidg.QLabel("Каналів:")
        nChanCBox = pswidg.QComboBox()
        nChanCBox.addItems(('1', '2', '3', '4'))
        nChanCBox.currentTextChanged.connect(self.updateChannelLayout)

        # Making on-the-fly adjustments possible
        self.baudrateCBox.currentTextChanged.connect(self.setUART)
        self.NBitsCBox.currentTextChanged.connect(self.setUART)
        self.parityCBox.currentTextChanged.connect(self.setUART)
        self.stopCBox.currentTextChanged.connect(self.setUART)

        # Separator line
        separator = pswidg.QFrame()
        separator.setFrameShape(pswidg.QFrame.Shape.HLine)

        # Grid layout for channels
        channels = pswidg.QWidget()
        self.channelsLayout = pswidg.QGridLayout(channels)

        # The channel components list contains dicts with the following structure:
        # widget - the widget handle of the channel box, for later destruction
        # discr - discretization frequency
        # scale - scaling coefficient
        # zeroOffset - zero offset
        # output - data display controller
        self.channelComponents = [] # used for later data retrieval

        # integration
        controlsLayout.addWidget(portLabel)
        controlsLayout.addWidget(self.portCBox)
        controlsLayout.addWidget(baudrateLabel)
        controlsLayout.addWidget(self.baudrateCBox)
        controlsLayout.addWidget(NBitsLabel)
        controlsLayout.addWidget(self.NBitsCBox)
        controlsLayout.addWidget(parityLabel)
        controlsLayout.addWidget(self.parityCBox)
        controlsLayout.addWidget(stopLabel)
        controlsLayout.addWidget(self.stopCBox)
        controlsLayout.addWidget(nChanLabel)        
        controlsLayout.addWidget(nChanCBox)

        primaryLayout.addWidget(controlsContainer)
        primaryLayout.addWidget(separator)
        primaryLayout.addWidget(channels)

        self.setCentralWidget(primaryContainer)
        self.createChannel(0)

        # Port list refresher
        self.refreshTimer = QTimer(self)
        self.refreshTimer.setInterval(500)
        self.refreshTimer.timeout.connect(self.refreshUART)
        self.refreshTimer.start()

        # UART communication handler thread
        self.threadPool = QThreadPool(self)
        self.UARTWorker = UARTWorker(self)
        self.threadPool.start(self.UARTWorker)

    def updateChannelLayout(self) -> None:
        required = int(self.sender().currentText())
        current = len(self.channelComponents)

        if(current > required):
            for i in range(required, current): # clean up excess channel components
                self.channelComponents[i]['widget'].deleteLater()
            for i in range(required, current): # destroy excess slots
                del self.channelComponents[required]
        elif(required > current):
            for i in range(current, required):
                self.createChannel(i) # create appropriate channels

    def createChannel(self, ident: int) -> None:
        comp = pswidg.QWidget()
        VBLayout = pswidg.QVBoxLayout(comp)
        chName = pswidg.QLabel(f'Канал №{ident+1}')
        output = pswidg.QLabel('Значення: ---') # Will be replaced by graphs in the future

        # subcontrols layout
        subctrl = pswidg.QWidget()
        subctrlHBox = pswidg.QHBoxLayout(subctrl)
        subctrlHBox.setAlignment(Qt.AlignmentFlag.AlignLeft)

        sub_discrLabel = pswidg.QLabel("Част. дискретизації:")
        sub_discr = pswidg.QLineEdit('0')
        sub_discr.setValidator(self.positiveOnly)
        sub_discr.setFixedWidth(INPUT_FIELDS_W)

        doublesOnly = QDoubleValidator() # Uses commas as float separator
        sub_scaleLabel = pswidg.QLabel("Масштаб:")
        sub_scale = pswidg.QLineEdit('1')
        sub_scale.setFixedWidth(INPUT_FIELDS_W)
        sub_scale.setValidator(doublesOnly)

        sub_zeroOffsetLabel = pswidg.QLabel("Зміщення:")
        sub_zeroOffset = pswidg.QLineEdit('0')
        sub_zeroOffset.setValidator(doublesOnly)
        sub_zeroOffset.setFixedWidth(INPUT_FIELDS_W)

        # subcontrols adding
        subctrlHBox.addWidget(sub_discrLabel)
        subctrlHBox.addWidget(sub_discr)
        subctrlHBox.addWidget(sub_scaleLabel)
        subctrlHBox.addWidget(sub_scale)
        subctrlHBox.addWidget(sub_zeroOffsetLabel)
        subctrlHBox.addWidget(sub_zeroOffset)

        # integration
        VBLayout.addWidget(chName)
        VBLayout.addWidget(subctrl)
        VBLayout.addWidget(output)

        self.channelsLayout.addWidget(comp, ident//2, ident%2) # positioning
        compdict = {
            "widget": comp,
            "discr": sub_discr,
            "scale": sub_scale,
            "zeroOffset": sub_zeroOffset,
            "output": output
        }
        self.channelComponents.append(compdict)

    def refreshUART(self) -> None:
        uarts = [DISCONNECT_LITERAL]

        for port in comports():
            uarts.append(port.name)
        
        present = [] # uarts currently present

        length = self.portCBox.count()
        for i in range(length):
            present.append(self.portCBox.itemText(i))

        to_remove = set(present)-set(uarts) # using sets prevents entry duplication
        to_add = set(uarts)-set(present)

        # first remove old elements, then add new ones
        for i in range(length):
            itext = self.portCBox.itemText(i)
            if itext in to_remove: self.portCBox.removeItem(i)

        for item in to_add:
            self.portCBox.addItem(item)

    def setUART(self) -> None:
        port = self.portCBox.currentText()
        baudrate = self.baudrateCBox.currentText()
        nBits = self.NBitsCBox.currentText()
        stopbits = self.stopCBox.currentText()
        parityMap = {
            "None" : serial.PARITY_NONE,
            "Even": serial.PARITY_EVEN,
            "Odd": serial.PARITY_ODD,
            "Mark": serial.PARITY_MARK,
            "Space": serial.PARITY_SPACE
        }

        if port == DISCONNECT_LITERAL:
            self.commandQueue.put_nowait('$CLOSE')
        else:
            self.commandQueue.put_nowait(f'{port} {baudrate} {nBits} {parityMap[self.parityCBox.currentText()]} {stopbits}')

    def cleanup(self) -> None: # releases UART ports, saves all files, etc when quitting the app
        # self.isRunning = False
        self.commandQueue.put_nowait('$QUIT')

class UARTWorker(QRunnable):
    def __init__(self, mainwindow: MainWindow) -> None:
        super().__init__()
        self.mainwindow = mainwindow

    @Slot()
    def run(self) -> None:
        isRunning = True
        while isRunning:

            if not self.mainwindow.commandQueue.empty():
                command = self.mainwindow.commandQueue.get_nowait()
                self.mainwindow.serial.close()
                print(f'> {command}')
                match command:
                    case '$CLOSE': pass
                    case '$QUIT': isRunning = False
                    case other:
                        params = other.split()
                        self.mainwindow.serial = serial.Serial(
                            port=params[0],
                            baudrate=int(params[1]),
                            bytesize=int(params[2]),
                            parity=params[3],
                            stopbits=float(params[4]),
                            timeout=5
                        )

            if(self.mainwindow.serial.is_open):
                databytes = self.mainwindow.serial.readline()
                datatext = str(databytes, encoding='ascii').strip()
                print(datatext)
                # self.mainwindow.channelComponents[0]['output'].setText(f'Значення {datatext}')
            else:
                sleep(0.1) # to prevent GUI performance sagging by the thread
        
main = pswidg.QApplication()

win = MainWindow()
win.show()

main.exec()
win.cleanup()