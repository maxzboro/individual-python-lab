import PySide6.QtWidgets as pswidg
from PySide6.QtGui import Qt, QFont
from PySide6.QtCore import QTimer
from globals import *

boldFont = QFont()
boldFont.setBold(True)

class CursorTool(pswidg.QMainWindow):
    def __init__(self, mainwindow: pswidg.QMainWindow) -> None:
        super().__init__()
        self.doSync = False
        self.mainwindow = mainwindow
        self.setWindowTitle('Інструмент "Курсори"')

        self.updateTimer = QTimer(self)
        self.updateTimer.setInterval(10)
        self.updateTimer.timeout.connect(self.updateChannelAttachments)
        self.updateTimer.start()

        container = pswidg.QWidget()
        self.componentGrid = pswidg.QGridLayout(container)
        self.componentGrid.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.syncCheckBox = pswidg.QCheckBox('Синхронізувати')
        self.componentGrid.addWidget(self.syncCheckBox,0,0)

        col = 0
        for heading in ['Назва каналу', 'Позиція 1', 'Позиція 2', 'Значення']:
            label = pswidg.QLabel(heading)
            label.setFont(boldFont)
            self.componentGrid.addWidget(label, 1, col)
            col+=1

        # stores lists of the following structure:
        # the cursor view widgets list for later deletion in the order [nameWidget, pos1, pos2, values] 
        # and additionally a tuple with the cursor times and values as the 5th elem
        # in the format [pos1, val1, pos2, val2]
        self.cursorComponents = []

        self.compare1 = pswidg.QComboBox()
        self.compare1.setFixedWidth(INPUT_FIELDS_W)
        self.compare2 = pswidg.QComboBox()
        self.compare2.setFixedWidth(INPUT_FIELDS_W)
        comparisonValues = ['']
        for i in range(1,5):
            comparisonValues.append(f'{i}.1')
            comparisonValues.append(f'{i}.2')
        self.compare1.addItems(comparisonValues)
        self.compare2.addItems(comparisonValues)

        self.compareOut = pswidg.QLabel('')

        self.componentGrid.addWidget(pswidg.QLabel('Порівняти:'), 6, 0)
        self.componentGrid.addWidget(self.compare1, 6, 1)
        self.componentGrid.addWidget(self.compare2, 6, 2)
        self.componentGrid.addWidget(self.compareOut, 6, 3)
        self.setCentralWidget(container)
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint)

    def updateChannelAttachments(self) -> None:
        required = len(self.mainwindow.channelComponents)
        current = len(self.cursorComponents)

        if(current > required):
            for i in range(required, current):
                for widget in self.cursorComponents[i][:-1]:
                    widget.deleteLater()
            for i in range(required, current):
                del self.cursorComponents[required]
        elif(required > current):
            for i in range(current, required):
                name = pswidg.QLabel(self.mainwindow.channelComponents[i]['chName'].text())
                pos1 = pswidg.QLineEdit()
                pos1.setFixedWidth(INPUT_FIELDS_W)
                pos1.setValidator(doublesOnly)
                pos2 = pswidg.QLineEdit()
                pos2.setFixedWidth(INPUT_FIELDS_W)
                pos2.setValidator(doublesOnly)
                values = pswidg.QLabel("Значення: ---")

                widgets = [name, pos1, pos2, values, (None, None, None, None)]
                col = 0
                for widget in widgets[:-1]:
                    self.componentGrid.addWidget(widget, i+2, col)
                    col+=1

                self.cursorComponents.append(widgets)

        for i in range(len(self.cursorComponents)):
            component = self.cursorComponents[i]
            if i>0:
                component[1].setReadOnly(self.syncCheckBox.isChecked())
                component[2].setReadOnly(self.syncCheckBox.isChecked())
                if(self.syncCheckBox.isChecked()):
                    component[1].setText(self.cursorComponents[0][1].text())
                    component[2].setText(self.cursorComponents[0][2].text())

            if len(self.mainwindow.channelComponents[i]['data']) == 0: continue

            maxtime = (len(self.mainwindow.channelComponents[i]['data'])-1)/self.mainwindow.channelComponents[i]['discr']
            component[0].setText(self.mainwindow.channelComponents[i]['chName'].text())

            try:
                pos1 = component[1].text().replace(',', '.'); pos1 = process_time_window(float(pos1), maxtime) if pos1 else None
                pos2 = component[2].text().replace(',', '.'); pos2 = process_time_window(float(pos2), maxtime) if pos2 else None
            except ValueError:
                pos1 = None
                pos2 = None

            self.mainwindow.channelComponents[i]['cursors'] = (pos1, pos2)
            val1 = None
            val2 = None
            output = '---'

            if not pos1 is None:
                val1 = self.mainwindow.channelComponents[i]['data'][int(pos1*self.mainwindow.channelComponents[i]['discr'])]
            if not pos2 is None:
                val2 = self.mainwindow.channelComponents[i]['data'][int(pos2*self.mainwindow.channelComponents[i]['discr'])]

            if pos1 == None and pos2 != None:
                output = f'{round(val2, 3)} {self.mainwindow.channelComponents[i]['unit']}'
                self.mainwindow.channelComponents[i]['cur1'].setData([],[])
                self.mainwindow.channelComponents[i]['cur2'].setData([pos2],[val2])
            elif pos1 != None and pos2 == None:
                output = f'{round(val1, 3)} {self.mainwindow.channelComponents[i]['unit']}'
                self.mainwindow.channelComponents[i]['cur1'].setData([pos1],[val1])
                self.mainwindow.channelComponents[i]['cur2'].setData([],[])
            elif pos1 != None and pos2 != None:
                output = f'{round(val1, 3)}, {round(val2, 3)} {self.mainwindow.channelComponents[i]['unit']}'
                self.mainwindow.channelComponents[i]['cur1'].setData([pos1],[val1])
                self.mainwindow.channelComponents[i]['cur2'].setData([pos2],[val2])
            else:
                self.mainwindow.channelComponents[i]['cur1'].setData([],[])
                self.mainwindow.channelComponents[i]['cur2'].setData([],[])

            component[3].setText(output)
            component[4] = (pos1, val1, pos2, val2)
        if(self.compare1.currentText() and self.compare2.currentText()):
            ch1 = int(self.compare1.currentText()[0])-1, int(self.compare1.currentText()[2])-1
            ch2 = int(self.compare2.currentText()[0])-1, int(self.compare2.currentText()[2])-1
            lenComponents = len(self.cursorComponents)
            if(ch1[0]>=lenComponents or ch2[0]>=lenComponents):
                self.compareOut.setText('⚠️ Не існує каналу.')
            else:
                comp1 = self.cursorComponents[ch1[0]]
                comp2 = self.cursorComponents[ch2[0]]
                pos1 = comp1[4][2*ch1[1]]
                val1 = comp1[4][1+2*ch1[1]]
                pos2 = comp2[4][2*ch2[1]]
                val2 = comp2[4][1+2*ch2[1]]

                if(pos1 is None or pos2 is None):
                    self.compareOut.setText('⚠️ Не існує курсора.')
                else:
                    self.compareOut.setText(f'∆t={round(pos2-pos1,3)} с; ∆y={round(val2-val1,3)}')


    def closeEvent(self, event):
        self.updateTimer.stop()
        for component in self.mainwindow.channelComponents:
            component['cur1'].setData([],[])
            component['cur2'].setData([],[])
        return super().closeEvent(event)