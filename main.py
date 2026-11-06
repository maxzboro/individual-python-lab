import PySide6.QtWidgets as pswidg
from PySide6.QtGui import Qt, QIntValidator, QDoubleValidator, QFont
from PySide6.QtCore import QTimer, QRunnable, Slot, QThreadPool
import serial
from serial.tools.list_ports import comports
from time import sleep
import queue
import pyqtgraph as pqtg

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

# pen = pqtg.mkPen(color=(255,255,255), width=2)

class MainWindow(pswidg.QMainWindow):
    def __init__(self) -> None:
        super().__init__()

        # Serial port instance and control
        self.serial = serial.Serial()
        self.commandQueue = queue.Queue() # to prevent race conditions such as trying to change the port during a read
        # UARTWorker commands (the $ prefix is used because there are no dollar signs in COM names)
        # ---
        # $CLOSE - close the serial connection;
        # $QUIT - the application is being closed, time to leave;
        # anything else - tries to connect to that port with the given parameters.

        self.dataQueue = queue.Queue() # to prevent the UARTWorker raising RuntimeErrors due to the widget already being destroyed

        self.setWindowTitle("Multichannel Experimental Data Reader")
        primaryContainer = pswidg.QWidget()
        primaryLayout = pswidg.QVBoxLayout(primaryContainer)
        primaryLayout.setAlignment(Qt.AlignmentFlag.AlignTop)

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
        self.baudrateCBox.setValidator(positiveOnly)

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

        # Text data separator
        sepLabel = pswidg.QLabel('Роздільник: ')
        self.separator = pswidg.QLineEdit()
        self.separator.setFixedWidth(INPUT_FIELDS_W)

        # Separator line
        guiSeparator = pswidg.QFrame()
        guiSeparator.setFrameShape(pswidg.QFrame.Shape.HLine)

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

        # Control panel integration
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
        controlsLayout.addWidget(sepLabel)
        controlsLayout.addWidget(self.separator)

        primaryLayout.addWidget(controlsContainer)
        primaryLayout.addWidget(guiSeparator)
        primaryLayout.addWidget(channels)

        self.setCentralWidget(primaryContainer)
        self.createChannel(0)

        self.status = pswidg.QStatusBar(self) # For displaying warning messages
        self.status.setSizeGripEnabled(False)
        self.setStatusBar(self.status)

        # Port list refresher
        self.refreshTimer = QTimer(self)
        self.refreshTimer.setInterval(100)
        self.refreshTimer.timeout.connect(self.refreshUART)
        self.refreshTimer.start()

        # UART communication handler thread
        self.threadPool = QThreadPool(self)
        self.UARTWorker = UARTWorker(self)
        self.threadPool.start(self.UARTWorker)

        # Data pull timer (timers terminate together with the MainWindow, so no RuntimeErrors occur)
        self.dataPullTimer = QTimer(self)
        self.dataPullTimer.setInterval(1)
        self.dataPullTimer.timeout.connect(self.pullData)
        self.dataPullTimer.start()

    def pullData(self) -> None:
        while not self.dataQueue.empty():
            databytes = self.dataQueue.get_nowait()
            sep = self.separator.text()
            try:
                dataIn = str(databytes, encoding='ascii').split(sep if sep else ' ')
                nChannels = len(self.channelComponents)

                for i in range(min(len(dataIn), nChannels)):
                    try:
                        inputRaw = float(dataIn[i]) # to raise ValueError as early as possible in case of wrong data
                        self.channelComponents[i]['data'].append(inputRaw)
                        self.updateChannelData(i)
                        self.status.showMessage('Дані прийнято без помилок.')
                    except ValueError:
                        self.status.showMessage(f'⚠️ Некоректні параметри або дані на вході каналу №{i+1}: {dataIn[i]}')

                if(len(dataIn)>nChannels):
                    self.status.showMessage(f'Забагато даних на вході, відображаються {nChannels}/{len(dataIn)} каналів.')
            except UnicodeDecodeError:
                self.status.showMessage(f'⚠️ Неможливо декодувати символи.')
    
    def updateChannelData(self, i: int) -> None:
        """Updates the given channel's data graph."""
        scaling = self.channelComponents[i]['scale']
        zeroOffset = self.channelComponents[i]['zeroOffset']

        xAxis = [x/(self.channelComponents[i]['discr'] if self.channelComponents[i]['discr'] else 1) for x in range(len(self.channelComponents[i]['data']))]
        yAxis = [y*scaling+zeroOffset for y in self.channelComponents[i]['data']]

        self.channelComponents[i]['line'].setData(xAxis, yAxis)

    def updateChannelLayout(self) -> None:
        """Expands or contracts the number of channels to match the amount required."""
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
        """Spawns a new channel widget with the given identifier, and adds it to the grid."""
        comp = pswidg.QWidget()
        VBLayout = pswidg.QVBoxLayout(comp)
        title = pswidg.QWidget()
        titleHBox = pswidg.QHBoxLayout(title)
        chName = pswidg.QLabel(f'Канал №{ident+1}')
        chName.setFont(importantFont)
        graph = pqtg.PlotWidget()
        graph.setLabel('left', 'Величина, од.')
        graph.setLabel('bottom', 'Час, сек.')
        graph.showGrid(True, True)
        line = graph.plot([],[],)# pen=pen)

        configSummoner = pswidg.QPushButton("Налаштувати...")
        configSummoner.setFixedWidth(INPUT_FIELDS_W*2)
        configSummoner.clicked.connect(lambda: self.summonConfig(ident))

        # integration
        titleHBox.addWidget(chName)
        titleHBox.addWidget(configSummoner)
        VBLayout.addWidget(title)
        VBLayout.addWidget(graph)

        self.channelsLayout.addWidget(comp, ident//2, ident%2) # positioning
        compdict = {
            "widget": comp,
            "chName": chName,
            "discr": 1,
            "scale": 1,
            "zeroOffset": 0,
            "unit": "од.",
            "graph": graph,
            "line": line,
            "data": []
        }
        self.channelComponents.append(compdict)

    def summonConfig(self, channel: int) -> None:
        """Summons a config window for a given channel."""
        self.cwindow = ConfigWindow(self, channel)
        self.cwindow.show()

    def refreshUART(self) -> None:
        """Updates the portCBox combobox to reflect the amount of UARTs available."""
        uarts = [DISCONNECT_LITERAL, TESTING_LITERAL]

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
        """Sends appropriate commands to the UARTWorker to update its config."""
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

class ConfigWindow(pswidg.QMainWindow):
    def __init__(self, mainwindow: MainWindow, channel: int) -> None:
        super().__init__()
        self.mainwindow = mainwindow
        self.channelNum = channel
        self.setWindowTitle(f"Налаштування каналу {channel+1}")

        # subcontrols layout
        subctrl = pswidg.QWidget()
        subctrlVBox = pswidg.QVBoxLayout(subctrl)
        subctrlVBox.setAlignment(Qt.AlignmentFlag.AlignLeft)
        titleLabel = pswidg.QLabel(f"Канал №{channel+1}:")
        titleLabel.setFont(importantFont)

        sub_chNameLabel = pswidg.QLabel("Назва каналу:")
        self.sub_chName = pswidg.QLineEdit(
            self.mainwindow.channelComponents[self.channelNum]['chName'].text()
        )
        self.sub_chName.textChanged.connect(self.updateChannel)

        sub_discrLabel = pswidg.QLabel("Частота дискретизації в Герцах:")
        self.sub_discr = pswidg.QLineEdit(
            str( self.mainwindow.channelComponents[self.channelNum]['discr'] ).replace('.', ',')
        )
        self.sub_discr.setValidator(positDoublesOnly)
        self.sub_discr.setFixedWidth(INPUT_FIELDS_W)
        self.sub_discr.textChanged.connect(self.updateChannel)

        sub_unitLabel = pswidg.QLabel("Одиниці вимірювання осі Y:")
        self.sub_unit = pswidg.QLineEdit(
            self.mainwindow.channelComponents[self.channelNum]['unit']
        )
        self.sub_unit.setFixedWidth(INPUT_FIELDS_W)
        self.sub_unit.textChanged.connect(self.updateChannel)

        sub_scaleLabel = pswidg.QLabel("Масштаб осі Y (можна й від'ємний):")
        self.sub_scale = pswidg.QLineEdit(
            str( self.mainwindow.channelComponents[self.channelNum]['scale'] ).replace('.', ',')
        )
        self.sub_scale.setFixedWidth(INPUT_FIELDS_W)
        self.sub_scale.setValidator(doublesOnly)
        self.sub_scale.textChanged.connect(self.updateChannel)

        sub_zeroOffsetLabel = pswidg.QLabel("Зміщення осі Y в заданих одиницях:")
        self.sub_zeroOffset = pswidg.QLineEdit(
            str( self.mainwindow.channelComponents[self.channelNum]['zeroOffset'] ).replace('.', ',')
        )
        self.sub_zeroOffset.setValidator(doublesOnly)
        self.sub_zeroOffset.setFixedWidth(INPUT_FIELDS_W)
        self.sub_zeroOffset.textChanged.connect(self.updateChannel)

        # subcontrols adding
        subctrlVBox.addWidget(titleLabel)
        subctrlVBox.addWidget(sub_chNameLabel)
        subctrlVBox.addWidget(self.sub_chName)
        subctrlVBox.addWidget(sub_discrLabel)
        subctrlVBox.addWidget(self.sub_discr)
        subctrlVBox.addWidget(sub_unitLabel)
        subctrlVBox.addWidget(self.sub_unit)
        subctrlVBox.addWidget(sub_scaleLabel)
        subctrlVBox.addWidget(self.sub_scale)
        subctrlVBox.addWidget(sub_zeroOffsetLabel)
        subctrlVBox.addWidget(self.sub_zeroOffset)
        self.setCentralWidget(subctrl)

    def updateChannel(self) -> None:
        discr = self.sub_discr.text() # the if value: set value thing is done to prevent empty fields from raising ValueErrors
        scale = self.sub_scale.text()
        zeroOffset = self.sub_zeroOffset.text()
        unit = self.sub_unit.text()
        chName = self.sub_chName.text()
        if discr: self.mainwindow.channelComponents[self.channelNum]['discr'] = float(discr.replace(',', '.'))
        if scale: self.mainwindow.channelComponents[self.channelNum]['scale'] = float(scale.replace(',', '.'))
        if zeroOffset: self.mainwindow.channelComponents[self.channelNum]['zeroOffset'] = float(zeroOffset.replace(',', '.'))
        if unit: 
            self.mainwindow.channelComponents[self.channelNum]['unit'] = unit
            self.mainwindow.channelComponents[self.channelNum]['graph'].setLabel('left', f"Величина, {unit}")
        if chName: self.mainwindow.channelComponents[self.channelNum]['chName'].setText(chName)
        self.mainwindow.updateChannelData(self.channelNum)

class UARTWorker(QRunnable):
    def __init__(self, mainwindow: MainWindow) -> None:
        super().__init__()
        self.mainwindow = mainwindow

    @Slot()
    def run(self) -> None:
        isRunning = True
        TestCounter = 1
        isTesting = False
        while isRunning:
            try:
                if not self.mainwindow.commandQueue.empty():
                    command = self.mainwindow.commandQueue.get_nowait()
                    self.mainwindow.serial.close()
                    print(f'> {command}')
                    match command:
                        case '$CLOSE': isTesting = False
                        case '$QUIT': isRunning = False
                        case other:
                            params = other.split()
                            isTesting = params[0] == TESTING_LITERAL
                            if(not isTesting):
                                if(len(params) == 5):
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
                    self.mainwindow.dataQueue.put_nowait(databytes)
                else:
                    if isTesting:
                        self.mainwindow.dataQueue.put_nowait(bytes(f'{TestCounter} {1/TestCounter} {TestCounter**2} {TestCounter**0.5}', encoding='ascii'))
                        TestCounter+=1
                    sleep(0.1) # to prevent GUI performance sagging by the thread
            except serial.SerialException as exc:
                self.mainwindow.serial.close()
                self.mainwindow.status.showMessage(f'⚠️ Помилка послідовного порту! {repr(exc)}')
        
main = pswidg.QApplication()

win = MainWindow()
win.show()

main.exec()
win.cleanup()