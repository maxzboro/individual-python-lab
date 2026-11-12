import PySide6.QtWidgets as pswidg
from PySide6.QtGui import Qt, QIntValidator, QDoubleValidator, QFont, QActionGroup, QAction
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

class MainWindow(pswidg.QMainWindow):
    def __init__(self) -> None:
        super().__init__()

        # Serial port instance and control
        self.serial = serial.Serial()
        self.commandQueue = queue.Queue() # to prevent race conditions such as trying to change the port during a read
        self.separator = ' '
        self.customBaudrate = 9600
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

        menubar = self.menuBar()
        fileMenu = menubar.addMenu('Файл')
        uartMenu = menubar.addMenu('UART')
        toolsMenu = menubar.addMenu('Інструменти') # for the future

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

        self.uartSubmenu: pswidg.QMenu = None
        self.disconnTrigger: QAction = None

        for key in uartgen_dict:
            ag = QActionGroup(self)
            ag.setExclusive(True)
            submenu = uartMenu.addMenu(key)
            if(key == 'Port'): 
                self.uartSubmenu = submenu

            for item in uartgen_dict[key]:
                action = submenu.addAction(item.replace('*', ''))
                if item == '*'+DISCONNECT_LITERAL: self.disconnTrigger = action
                action.setCheckable(True)
                ag.addAction(action)
                if item[0] == '*': action.setChecked(True)

            self.uartConfigs[key] = ag
            ag.triggered.connect(self.updateChannelLayout if key=='Channels' else self.setUART)

        # Separator setter
        setSeparator = uartMenu.addAction('Задати роздільник...')
        setSeparator.triggered.connect(self.setSeparatorDialog)
        setCustomBaud = uartMenu.addAction('Нестандартний бодрейт...')
        setCustomBaud.triggered.connect(self.setBaudrateDialog)

        # Grid layout for channels
        channels = pswidg.QWidget()
        self.channelsLayout = pswidg.QGridLayout(channels)

        # The channel components list contains dicts with the following structure:
        # widget - the widget handle of the channel box, for later destruction
        # discr - discretization frequency
        # scale - scaling coefficient
        # zeroOffset - zero offset
        # output - data display controller
        # FIXME THIS LIST IS OUTDATED!!!
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
        self.dataPullTimer.setInterval(1)
        self.dataPullTimer.timeout.connect(self.pullData)
        self.dataPullTimer.start()

    def setSeparatorDialog(self) -> None:
        self.sepDiag = TextInputDialog(self, 'separator', "Введіть роздільник", \
                                        "Введіть новий роздільник:", default=self.separator)
        self.sepDiag.show()

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

        tw1 = self.channelComponents[i]['timeWindow1']
        self.channelComponents[i]['line'].getViewBox().setXRange(
            self.channelComponents[i]['timeWindow0'],
            tw1 if tw1!=-1 else xAxis[-1]
        )

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
        line = graph.plot([],[],)

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
            "data": [],
            "timeWindow0": 0,
            "timeWindow1": -1
        }
        line.getViewBox().register(f'Канал №{ident+1}')
        self.channelComponents.append(compdict)

    def summonConfig(self, channel: int) -> None:
        """Summons a config window for a given channel."""
        self.cwindow = ConfigWindow(self, channel)
        self.cwindow.show()

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
                self.uartSubmenu.removeAction(port)

        for port_name in sorted(to_add): # for aesthetic purposes
            port = self.uartSubmenu.addAction(port_name)
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

    def cleanup(self) -> None: # releases UART ports, saves all files, etc when quitting the app
        # self.isRunning = False
        self.commandQueue.put_nowait('$QUIT')

class ConfigWindow(pswidg.QMainWindow):
    def __init__(self, mainwindow: MainWindow, channel: int) -> None:
        # TODO Optimize window creation routines!!
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

        self.sub_timeWindow0Label = pswidg.QLabel("Початок часового вікна (порожній - початок відліку):")
        self.sub_timeWindow0 = pswidg.QLineEdit(
            str( self.mainwindow.channelComponents[self.channelNum]['timeWindow0'] ).replace('.', ',')
        )
        self.sub_timeWindow0.setFixedWidth(INPUT_FIELDS_W)
        self.sub_timeWindow0.setValidator(positDoublesOnly)
        self.sub_timeWindow0.textChanged.connect(self.updateChannel)

        self.sub_timeWindow1Label = pswidg.QLabel("Кінець часового вікна (порожній - остання точка даних):")
        tw1 = self.mainwindow.channelComponents[self.channelNum]['timeWindow1']
        self.sub_timeWindow1 = pswidg.QLineEdit(
            str(tw1).replace('.', ',') if tw1!=-1 else ''
        )
        self.sub_timeWindow1.setFixedWidth(INPUT_FIELDS_W)
        self.sub_timeWindow1.setValidator(positDoublesOnly)
        self.sub_timeWindow1.textChanged.connect(self.updateChannel)

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
        subctrlVBox.addWidget(self.sub_timeWindow0Label)
        subctrlVBox.addWidget(self.sub_timeWindow0)
        subctrlVBox.addWidget(self.sub_timeWindow1Label)
        subctrlVBox.addWidget(self.sub_timeWindow1)
        self.setCentralWidget(subctrl)

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
        else: self.mainwindow.channelComponents[self.channelNum]['timeWindow1'] = -1
        self.mainwindow.updateChannelData(self.channelNum)

class TextInputDialog(pswidg.QMainWindow):
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