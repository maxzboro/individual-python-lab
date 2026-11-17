import PySide6.QtWidgets as pswidg
from PySide6.QtGui import Qt, QActionGroup, QAction
from PySide6.QtCore import QTimer, QRunnable, Slot, QThreadPool
import serial
from serial.tools.list_ports import comports
from time import sleep
import queue
import pyqtgraph as pqtg
from math import sin
from globals import *
from tools import CursorTool
import struct

class MainWindow(pswidg.QMainWindow):
    def __init__(self) -> None:
        super().__init__()

        # Serial port instance and control
        self.serial = serial.Serial()
        self.commandQueue = queue.Queue() # to prevent race conditions such as trying to change the port during a read
        self.separator = ' '
        self.encoding = 'ascii'
        self.isBinaryMode = False
        self.binFormat = list('dddd')
        self.endianness = 'Native'
        self.customBaudrate = 9600
        # UARTWorker commands (the $ prefix is used because there are no dollar signs in COM names)
        # ---
        # $CLOSE - close the serial connection;
        # $QUIT - the application is being closed, time to leave;
        # anything else - tries to connect to that port with the given parameters.

        self.dataQueue = queue.Queue() # to prevent the UARTWorker raising RuntimeErrors due to the widget already being destroyed
        self.curtool = None # window handle stubs, to be later destroyed by cleanup
        self.cwindow = None
        self.fmtEditor = None
        self.rateDiag = None

        self.setWindowTitle("Multichannel Experimental Data Reader")
        primaryContainer = pswidg.QWidget()
        primaryLayout = pswidg.QVBoxLayout(primaryContainer)
        primaryLayout.setAlignment(Qt.AlignmentFlag.AlignTop)

        menubar = self.menuBar()
        fileMenu = menubar.addMenu('Файл')
        uartMenu = menubar.addMenu('UART')
        toolsMenu = menubar.addMenu('Інструменти')

        toolsMenu.addAction("Курсори").triggered.connect(self.summonCursorTool) # Cursor picker and analyzer tool

        fileMenu.addAction('Вийти').triggered.connect(self.close)

        # The UARTGen dict is as follows: Submenu name: (possible values, among which is the default)
        # Items that start with asterisks are set to be default.
        # Fun fact: the previous implementation used up twice as much lines of code and was more confusing to maintain.
        self.uartConfigs: dict[str, QActionGroup] = {}

        uartgen_dict = {
            'Port': ('*'+DISCONNECT_LITERAL, TESTING_LITERAL),
            'Baudrate': ('50', '75', '110', '134', '150', '200', '300', '600', '1200', '1800', \
            '2400', '4800', '*9600', '19200', '28800', '38400', '57600', '76800', \
            '115200', '230400', '460800', '576000', '921600'),
            'Data bits': ('*8','7','6','5'),
            'Parity': ('*None', 'Even', 'Odd', 'Mark', 'Space'),
            'Stop bits': ('*1', '2', '1.5'),
            'Channels': ('*1', '2', '3', '4')
        }

        self.portSubmenu: pswidg.QMenu = None
        self.disconnTrigger: QAction = None

        for key in uartgen_dict:
            ag = QActionGroup(self)
            ag.setExclusive(True)
            submenu = uartMenu.addMenu(key)
            if(key == 'Port'): 
                self.portSubmenu = submenu

            for item in uartgen_dict[key]:
                action = submenu.addAction(item.replace('*', ''))
                if item == '*'+DISCONNECT_LITERAL: self.disconnTrigger = action
                action.setCheckable(True)
                ag.addAction(action)
                if item[0] == '*': action.setChecked(True)

            self.uartConfigs[key] = ag
            ag.triggered.connect(self.updateChannelLayout if key=='Channels' else self.setUART)

        # Separator setter
        setFormat = uartMenu.addAction('Задати формат...')
        setFormat.triggered.connect(self.summonFormatEditor)
        setCustomBaud = uartMenu.addAction('Нестандартний бодрейт...')
        setCustomBaud.triggered.connect(self.setBaudrateDialog)

        # Grid layout for channels
        channels = pswidg.QWidget()
        self.channelsLayout = pswidg.QGridLayout(channels)

        self.channelComponents = [] # used for later data retrieval

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
        self.dataPullTimer.setInterval(5)
        self.dataPullTimer.timeout.connect(self.pullData)
        self.dataPullTimer.start()

    def summonFormatEditor(self) -> None:
        self.fmtEditor = FormatEditor(self)
        self.fmtEditor.show()

    def setBaudrateDialog(self) -> None:
        self.rateDiag = TextInputDialog(self, 'customBaudrate', "Введіть бодрейт", \
                                        "Введіть швидкість передачі даних", numtype='n',\
                                        default=str(self.customBaudrate), after=self.setBaudrateAfter)
        self.rateDiag.show()

    def setBaudrateAfter(self) -> None:
        self.uartConfigs['Baudrate'].checkedAction().setChecked(False)
        self.setUART()

    def pullData(self) -> None:
        while not self.dataQueue.empty():
            databytes = self.dataQueue.get_nowait()
            sep = self.separator
            try:
                if self.isBinaryMode:
                    fmt = ENDIAN_DICT[self.endianness]
                    for x in self.binFormat:
                        fmt+=x
                    dataIn = struct.unpack(fmt, databytes)
                else:
                    dataIn = str(databytes, encoding=self.encoding).split(sep if sep else ' ')
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
        if len(self.channelComponents[i]['data']) == 0: return
        scaling = self.channelComponents[i]['scale']
        zeroOffset = self.channelComponents[i]['zeroOffset']

        xAxis = [x/(self.channelComponents[i]['discr'] if self.channelComponents[i]['discr'] else 1) for x in range(len(self.channelComponents[i]['data']))]
        yAxis = [y*scaling+zeroOffset for y in self.channelComponents[i]['data']]

        maxtime = (len(self.channelComponents[i]['data'])-1)/self.channelComponents[i]['discr']

        tw0 = process_time_window(self.channelComponents[i]['timeWindow0'], maxtime)
        tw1 = process_time_window(self.channelComponents[i]['timeWindow1'], maxtime)
        self.channelComponents[i]['line'].getViewBox().setXRange(tw0, tw1)

        self.channelComponents[i]['line'].setData(xAxis, yAxis)

    def updateChannelLayout(self) -> None:
        """Expands or contracts the number of channels to match the amount required."""
        required = int(self.uartConfigs['Channels'].checkedAction().text())
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
        line = graph.plot([],[])
        cur1 = graph.plot([],[], symbol='t')
        cur2 = graph.plot([],[], symbol='s')

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
            "discr": 10,
            "scale": 1,
            "zeroOffset": 0,
            "unit": "од.",
            "graph": graph,
            "line": line,
            "data": [],
            "timeWindow0": 0,
            "timeWindow1": None,
            "cur1": cur1,
            "cur2": cur2
        }
        line.getViewBox().register(f'Канал №{ident+1}')
        self.channelComponents.append(compdict)

    def summonConfig(self, channel: int) -> None:
        """Summons a config window for a given channel."""
        self.cwindow = ConfigWindow(self, channel)
        self.cwindow.show()

    def summonCursorTool(self) -> None:
        self.curtool = CursorTool(self)
        self.curtool.show()

    def refreshUART(self) -> None:
        """Updates the portCBox combobox to reflect the amount of UARTs available."""
        uarts = [DISCONNECT_LITERAL, TESTING_LITERAL]
        currentPort = self.uartConfigs['Port'].checkedAction().text()

        for port in comports():
            uarts.append(port.name)
        
        present = [] # uarts currently present

        for port in self.uartConfigs['Port'].actions():
            present.append(port.text())

        to_remove = set(present)-set(uarts) # using sets prevents entry duplication
        to_add = set(uarts)-set(present)

        for port in self.uartConfigs['Port'].actions():
            port_name = port.text()
            if port_name in to_remove:
                if port_name == currentPort:
                    self.disconnTrigger.trigger() # Previously impossible
                self.uartConfigs['Port'].removeAction(port)
                self.portSubmenu.removeAction(port)

        for port_name in sorted(to_add): # for aesthetic purposes
            port = self.portSubmenu.addAction(port_name)
            port.setCheckable(True)
            self.uartConfigs['Port'].addAction(port)

    def setUART(self) -> None:
        """Sends appropriate commands to the UARTWorker to update its config."""
        port = self.uartConfigs['Port'].checkedAction().text()
        if self.uartConfigs['Baudrate'].checkedAction():
            baudrate = self.uartConfigs['Baudrate'].checkedAction().text()
            self.customBaudrate = int(baudrate)
        else:
            baudrate = self.customBaudrate
        nBits = self.uartConfigs['Data bits'].checkedAction().text()
        stopbits = self.uartConfigs['Stop bits'].checkedAction().text()
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
            self.commandQueue.put_nowait(f'{port} {baudrate} {nBits} {parityMap[self.uartConfigs['Parity'].checkedAction().text()]} {stopbits}')

    def closeEvent(self, event):
        self.commandQueue.put_nowait('$QUIT')
        for win in [self.curtool,self.cwindow,self.fmtEditor,self.rateDiag]:
            if win: win.close()
        return super().closeEvent(event)

class FormatEditor(pswidg.QMainWindow):
    def __init__(self, mainwindow: MainWindow) -> None:
        super().__init__()
        self.mainwindow = mainwindow
        self.setWindowTitle(f'Налаштування формату введення/виведення')
        mainContainer = pswidg.QWidget()
        self.mainGrid = pswidg.QGridLayout(mainContainer)
        self.mainGrid.setAlignment(Qt.AlignmentFlag.AlignTop)
        modeSetLabel = pswidg.QLabel('Режим прийому даних:')
        self.modeCBox = pswidg.QComboBox()
        self.modeCBox.addItems(['Текстовий', 'Двійковий'])
        self.modeCBox.setCurrentIndex(int(self.mainwindow.isBinaryMode))
        self.modeCBox.currentTextChanged.connect(self.updateConfigListings)
        self.modeCBox.setFixedWidth(INPUT_FIELDS_W*1.5)
        self.mainGrid.addWidget(modeSetLabel, 0, 0)
        self.mainGrid.addWidget(self.modeCBox, 0, 1)

        # holds references to config items as pairs (label, value)
        self.configWidgets = []

        self.setCentralWidget(mainContainer)
        self.updateConfigListings()
        self.setWindowFlags(Qt.WindowType.WindowStaysOnTopHint)

    def updateConfigListings(self) -> None:
        for pair in self.configWidgets:
            pair[0].deleteLater()
            pair[1].deleteLater()
        self.configWidgets = []

        self.mainwindow.isBinaryMode = self.modeCBox.currentText() == 'Двійковий'
        if not self.mainwindow.isBinaryMode:
            lbl = pswidg.QLabel('Роздільник:')
            separator = pswidg.QLineEdit(self.mainwindow.separator)
            separator.setFixedWidth(INPUT_FIELDS_W)
            self.mainGrid.addWidget(lbl, 1, 0)
            self.mainGrid.addWidget(separator, 1, 1)
            separator.textChanged.connect(self.setTextSeparator)

            encoderLbl = pswidg.QLabel('Кодування: ')
            encoding = pswidg.QLineEdit(self.mainwindow.encoding)
            encoding.setFixedWidth(INPUT_FIELDS_W)
            self.mainGrid.addWidget(encoderLbl, 2, 0)
            self.mainGrid.addWidget(encoding, 2, 1)
            encoding.textChanged.connect(self.setEncoding)

            self.configWidgets = [(lbl, separator), (encoderLbl, encoding)]
        else:
            for i in range(1, 5):
                lbl = pswidg.QLabel(f'Тип даних каналу {i}:')
                typeCBox = pswidg.QComboBox()
                typeCBox.addItems(BINARY_FORMATS.keys())
                typeCBox.chnlIndex = i-1
                typeCBox.setCurrentText(BINARY_FORMATS_INV[self.mainwindow.binFormat[i-1]])
                typeCBox.currentTextChanged.connect(self.setBinFormat)
                self.mainGrid.addWidget(lbl, i, 0)
                self.mainGrid.addWidget(typeCBox, i, 1)
                self.configWidgets.append((lbl, typeCBox))
            endiannessLbl = pswidg.QLabel('Порядок байтів:')
            endiannessCBox = pswidg.QComboBox()
            endiannessCBox.addItems(['Native', 'Big-Endian', 'Little-Endian'])
            endiannessCBox.setCurrentText(self.mainwindow.endianness)
            endiannessCBox.currentTextChanged.connect(self.setEndianness)
            self.configWidgets.append((endiannessLbl, endiannessCBox))
            self.mainGrid.addWidget(endiannessLbl, 5, 0)
            self.mainGrid.addWidget(endiannessCBox, 5, 1)

    def setBinFormat(self) -> None:
        self.mainwindow.binFormat[self.sender().chnlIndex] = BINARY_FORMATS[self.sender().currentText()]

    def setEndianness(self) -> None:
        self.mainwindow.endianness = self.sender().currentText()

    def setTextSeparator(self) -> None:
        self.mainwindow.separator = self.sender().text()

    def setEncoding(self) -> None:
        self.mainwindow.encoding = self.sender().text()

class ConfigWindow(pswidg.QMainWindow):
    def __init__(self, mainwindow: MainWindow, channel: int) -> None:
        # TODO Optimize window creation routines!!
        super().__init__()
        self.mainwindow = mainwindow
        self.channelNum = channel
        self.setWindowTitle(f"Налаштування каналу {channel+1}")

        # subcontrols layout
        subctrl = pswidg.QWidget()
        subctrlGridBox = pswidg.QGridLayout(subctrl)
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

        self.sub_timeWindow0Label = pswidg.QLabel("Початок часового вікна (порожній - початок відліку):")
        self.sub_timeWindow0 = pswidg.QLineEdit(
            str( self.mainwindow.channelComponents[self.channelNum]['timeWindow0'] ).replace('.', ',')
        )
        self.sub_timeWindow0.setFixedWidth(INPUT_FIELDS_W)
        self.sub_timeWindow0.setValidator(doublesOnly)
        self.sub_timeWindow0.textChanged.connect(self.updateChannel)

        self.sub_timeWindow1Label = pswidg.QLabel("Кінець часового вікна (порожній - остання точка даних):")
        tw1 = self.mainwindow.channelComponents[self.channelNum]['timeWindow1']
        self.sub_timeWindow1 = pswidg.QLineEdit(
            str(tw1).replace('.', ',') if tw1!=None else ''
        )
        self.sub_timeWindow1.setFixedWidth(INPUT_FIELDS_W)
        self.sub_timeWindow1.setValidator(doublesOnly)
        self.sub_timeWindow1.textChanged.connect(self.updateChannel)

        # subcontrols adding
        subctrlGridBox.addWidget(titleLabel,0,0)
        subctrlGridBox.addWidget(sub_chNameLabel,1,0)
        subctrlGridBox.addWidget(self.sub_chName,1,1)
        subctrlGridBox.addWidget(sub_discrLabel,2,0)
        subctrlGridBox.addWidget(self.sub_discr,2,1)
        subctrlGridBox.addWidget(sub_unitLabel,3,0)
        subctrlGridBox.addWidget(self.sub_unit,3,1)
        subctrlGridBox.addWidget(sub_scaleLabel,4,0)
        subctrlGridBox.addWidget(self.sub_scale,4,1)
        subctrlGridBox.addWidget(sub_zeroOffsetLabel,5,0)
        subctrlGridBox.addWidget(self.sub_zeroOffset,5,1)
        subctrlGridBox.addWidget(self.sub_timeWindow0Label,6,0)
        subctrlGridBox.addWidget(self.sub_timeWindow0,6,1)
        subctrlGridBox.addWidget(self.sub_timeWindow1Label,7,0)
        subctrlGridBox.addWidget(self.sub_timeWindow1,7,1)
        self.setCentralWidget(subctrl)
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint)
        self.setFixedSize(self.sizeHint())

    def updateChannel(self) -> None:
        discr = self.sub_discr.text() # the if value: set value thing is done to prevent empty fields from raising ValueErrors
        scale = self.sub_scale.text()
        zeroOffset = self.sub_zeroOffset.text()
        unit = self.sub_unit.text()
        chName = self.sub_chName.text()
        tw0 = self.sub_timeWindow0.text()
        tw1 = self.sub_timeWindow1.text()
        if discr: self.mainwindow.channelComponents[self.channelNum]['discr'] = float(discr.replace(',', '.'))
        if scale: self.mainwindow.channelComponents[self.channelNum]['scale'] = float(scale.replace(',', '.'))
        if zeroOffset: self.mainwindow.channelComponents[self.channelNum]['zeroOffset'] = float(zeroOffset.replace(',', '.'))
        if unit: 
            self.mainwindow.channelComponents[self.channelNum]['unit'] = unit
            self.mainwindow.channelComponents[self.channelNum]['graph'].setLabel('left', f"Величина, {unit}")
        if chName: 
            self.mainwindow.channelComponents[self.channelNum]['chName'].setText(chName)
            self.mainwindow.channelComponents[self.channelNum]['line'].getViewBox().register(chName) # FIXME works poorly with renaming channels
        if tw0: self.mainwindow.channelComponents[self.channelNum]['timeWindow0'] = float(tw0.replace(',', '.'))
        else: self.mainwindow.channelComponents[self.channelNum]['timeWindow0'] = 0
        if tw1: self.mainwindow.channelComponents[self.channelNum]['timeWindow1'] = float(tw1.replace(',', '.'))
        else: self.mainwindow.channelComponents[self.channelNum]['timeWindow1'] = None
        self.mainwindow.updateChannelData(self.channelNum)

class TextInputDialog(pswidg.QMainWindow):
    """A dialog window that asks the user to input a given piece of text and saves it to an attribute of the main window.
    You may optionally attach a validator for numeric types and a function to be called after the user hits OK.  
    ### Arguments:  
    mainwindow: The main window instance.  
    attrname: The name of the MainWindow attribute which will be modified thru __setattr__.  
    title: The window name.  
    desc: The description of the prompt.  
    numtype: The type of number to set filtering. "n" is a natural num, "+" is a positive double, "." is any double.  
    default: The default value of the prompt.  
    after: The function to be called after the user hits OK. Must take no mandatory arguments."""
    def __init__(self, mainwindow: MainWindow, attrname: str, title: str, desc: str,\
                  numtype: str=None, default: str=None, after=None) -> None:
        super().__init__()
        self.mainwindow = mainwindow
        self.attrname = attrname
        self.numtype = numtype
        self.after = after
        self.setWindowTitle(title)
        mainContainer = pswidg.QWidget()
        mainLayout = pswidg.QVBoxLayout(mainContainer)
        mainLayout.setAlignment(Qt.AlignmentFlag.AlignLeft)
        helpText = pswidg.QLabel(desc)
        self.sepInput = pswidg.QLineEdit(default if default else '')
        if numtype == 'n': # Positive ints only
            self.sepInput.setValidator(positiveOnly)
        elif numtype == '+': # Positive doubles only
            self.sepInput.setValidator(positDoublesOnly)
        elif numtype == '.': # Doubles only
            self.sepInput.setValidator(doublesOnly)
        okBtn = pswidg.QPushButton('Підтвердити')
        okBtn.clicked.connect(self.finalize)
        mainLayout.addWidget(helpText)
        mainLayout.addWidget(self.sepInput)
        mainLayout.addWidget(okBtn)
        self.setCentralWidget(mainContainer)
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint)

    def finalize(self) -> None:
        final = self.sepInput.text()
        if self.numtype == 'n':
            final = int(final)
        elif self.numtype != None:
            final = float(final)
        self.mainwindow.__setattr__(self.attrname, final)
        self.close()
        if self.after:
            self.after()


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
                        if self.mainwindow.isBinaryMode:
                            fmt = ENDIAN_DICT[self.endianness]
                            for x in self.mainwindow.binFormat:
                                fmt+=x
                            self.mainwindow.dataQueue.put_nowait(struct.pack(fmt, TestCounter, 1/TestCounter, TestCounter**2, sin(TestCounter*0.05)))
                        else:
                            self.mainwindow.dataQueue.put_nowait(bytes(f'{TestCounter} {1/TestCounter} {TestCounter**2} {sin(TestCounter*0.05)}', encoding='ascii'))
                        TestCounter+=1
                    sleep(0.1) # to prevent GUI performance sagging by the thread
            except serial.SerialException as exc:
                self.mainwindow.serial.close()
                self.mainwindow.status.showMessage(f'⚠️ Помилка послідовного порту! {repr(exc)}')
        
main = pswidg.QApplication()

win = MainWindow()
win.show()

main.exec()