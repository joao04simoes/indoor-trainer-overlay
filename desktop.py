"""Janela inicial e barra de treino para leitores externos como o Stremio."""
import asyncio
from importlib import import_module
import sys
from pathlib import Path

from overlays import mmss
from workout_profile import create_profile


def qt():
    def load():
        return tuple(import_module('PyQt5.' + name) for name in ('QtCore', 'QtGui', 'QtWidgets'))
    try:
        return load()
    except ImportError:
        system = '/usr/lib/python3/dist-packages'
        if sys.platform.startswith('linux') and Path(system).is_dir():
            if system not in sys.path:
                sys.path.append(system)
        try:
            return load()
        except ImportError as exc:
            raise RuntimeError('A interface requer PyQt5: pip install PyQt5') from exc


def application():
    _, _, W = qt()
    app = W.QApplication.instance() or W.QApplication(['Indoor Trainer'])
    app.setQuitOnLastWindowClosed(False)
    return app


def power_color(power, target):
    if target is None or power is None:
        return '#ff9f1c'
    error = abs(power - target)
    if error <= target * .05 + 5:
        return '#ff9f1c'
    if error <= target * .15 + 10:
        return '#ffbd45'
    return '#ff6262'


def setup_session(args):
    C, _, W = qt()
    app = application()

    class Scanner(C.QThread):
        found = C.pyqtSignal(object)
        failed = C.pyqtSignal(str)

        def run(self):
            async def discover():
                from bleak import BleakScanner
                return await BleakScanner.discover(timeout=8, return_adv=True)
            try:
                self.found.emit(asyncio.run(discover()))
            except Exception as exc:
                self.failed.emit(str(exc))

    dialog = W.QDialog()
    dialog.setWindowTitle('Indoor Trainer — preparar sessão')
    dialog.resize(640, 480)
    layout = W.QFormLayout(dialog)
    mode = W.QComboBox()
    mode.addItems(['Treino livre', 'Potência constante', 'Ficheiro FIT'])
    mode.setCurrentIndex(2 if args.workout else 1 if args.power is not None else 0)
    layout.addRow('Tipo de treino', mode)
    file_row = W.QWidget()
    file_layout = W.QHBoxLayout(file_row)
    file_layout.setContentsMargins(0, 0, 0, 0)
    file_edit = W.QLineEdit(args.workout or '')
    browse = W.QPushButton('Escolher…')
    file_layout.addWidget(file_edit)
    file_layout.addWidget(browse)
    def choose():
        path, _ = W.QFileDialog.getOpenFileName(dialog, 'Escolher treino', '', 'Treinos (*.fit)')
        if path:
            file_edit.setText(path)
    browse.clicked.connect(choose)
    layout.addRow('Treino FIT', file_row)
    power = W.QSpinBox()
    power.setRange(0, 2000)
    power.setSpecialValueText('Sem objetivo')
    power.setSuffix(' W')
    power.setValue(args.power or 0)
    layout.addRow('Potência inicial', power)
    duration = W.QSpinBox()
    duration.setRange(0, 1440)
    duration.setSpecialValueText('Sem limite de tempo')
    duration.setSuffix(' min')
    duration.setValue(args.duration or 0)
    layout.addRow('Duração (livre / constante)', duration)
    ftp = W.QSpinBox()
    ftp.setRange(1, 2000)
    ftp.setValue(args.ftp)
    ftp.setSuffix(' W')
    layout.addRow('FTP', ftp)
    trainer = W.QComboBox()
    trainer.setEditable(True)
    trainer.addItem(args.address or args.trainer or '')
    trainer.lineEdit().setPlaceholderText('Automático, nome ou endereço Bluetooth')
    layout.addRow('Rolo FTMS', trainer)
    hr = W.QComboBox()
    hr.setEditable(True)
    hr.addItem('Não usar cinta', None)
    hr.addItem('Procurar automaticamente', '')
    if args.hr is not None:
        hr.setCurrentIndex(1)
        if args.hr:
            hr.addItem(args.hr, args.hr)
            hr.setCurrentIndex(2)
    layout.addRow('Cinta cardíaca', hr)
    scan = W.QPushButton('Procurar dispositivos Bluetooth')
    status = W.QLabel('Escolhe um dispositivo ou escreve o nome / endereço.')
    status.setWordWrap(True)
    layout.addRow(scan)
    layout.addRow(status)
    worker = Scanner(dialog)
    def discovered(devices):
        from devices import FTMS, HRS
        for device, ad in devices.values():
            services = [u.lower() for u in ad.service_uuids]
            label = f'{device.name or "Sem nome"} [{device.address}]'
            if FTMS in services:
                trainer.addItem(label, device.address)
            if HRS in services:
                hr.addItem(label, device.address)
        status.setText('Pesquisa concluída. Seleciona o rolo e, se quiseres, a cinta.')
        scan.setEnabled(True)
    def search():
        scan.setEnabled(False)
        status.setText('A procurar durante 8 segundos…')
        worker.start()
    def failed(message):
        status.setText('Erro Bluetooth: ' + message)
        scan.setEnabled(True)
    scan.clicked.connect(search)
    worker.found.connect(discovered)
    worker.failed.connect(failed)
    sim = W.QCheckBox('Simulação — testar sem dispositivos')
    sim.setChecked(args.sim)
    layout.addRow(sim)
    buttons = W.QDialogButtonBox(W.QDialogButtonBox.Ok | W.QDialogButtonBox.Cancel)
    buttons.button(W.QDialogButtonBox.Ok).setText('Iniciar treino')
    layout.addRow(buttons)
    def accept():
        if mode.currentIndex() == 2:
            try:
                from workouts import load_workout
                load_workout(file_edit.text(), ftp.value())
            except Exception as exc:
                W.QMessageBox.warning(dialog, 'Treino inválido', str(exc))
                return
        dialog.accept()
    buttons.accepted.connect(accept)
    buttons.rejected.connect(dialog.reject)
    def mode_changed():
        file_row.setEnabled(mode.currentIndex() == 2)
        power.setEnabled(mode.currentIndex() == 1)
        duration.setEnabled(mode.currentIndex() != 2)
    mode.currentIndexChanged.connect(mode_changed)
    mode_changed()
    accepted = dialog.exec_() == W.QDialog.Accepted
    # O thread deve terminar antes de destruir a janela.
    while worker.isRunning():
        app.processEvents()
        worker.wait(25)
    if not accepted:
        return None
    args.workout = file_edit.text() if mode.currentIndex() == 2 else None
    args.power = power.value() or None if mode.currentIndex() == 1 else None
    args.duration = duration.value() or None
    args.ftp = ftp.value()
    selection = trainer.currentData() if trainer.currentText() == trainer.itemText(trainer.currentIndex()) else None
    text = trainer.currentText().strip()
    args.address = selection or (text if ':' in text else None)
    args.trainer = text if text and not args.address else None
    args.hr = hr.currentData() if hr.currentText() == hr.itemText(hr.currentIndex()) else hr.currentText().strip()
    args.sim = sim.isChecked()
    args.overlay = args.yes = True
    return args


class DesktopOverlay:
    def __init__(self):
        C, G, W = qt()
        self.app = application()
        self.controls = {'paused': False, 'stop': False, 'delta': 0}
        self.menu = None

        class Window(W.QWidget):
            def closeEvent(window, event):
                event.ignore()
                self.open_menu()

            def keyPressEvent(window, event):
                if event.key() == C.Qt.Key_Escape:
                    self.open_menu()
                elif event.key() in (C.Qt.Key_Plus, C.Qt.Key_Equal):
                    self.adjust(5)
                elif event.key() == C.Qt.Key_Minus:
                    self.adjust(-5)

            def mousePressEvent(window, event):
                if event.button() == C.Qt.RightButton:
                    self.open_menu()

        self.root = Window()
        self.root.setWindowFlags(C.Qt.FramelessWindowHint | C.Qt.WindowStaysOnTopHint | C.Qt.Tool)
        self.root.setAttribute(C.Qt.WA_TranslucentBackground)
        self.root.setWindowTitle('Indoor Trainer')
        self.root.setStyleSheet('QWidget { background: transparent; color: #eeeeee; } QPushButton { padding: 3px 12px; background: rgba(20, 27, 37, 175); border: 1px solid #ff9f1c; border-radius: 4px; color: #ffad33; }')
        screen = self.app.primaryScreen().geometry()
        self.root.setGeometry(screen.x(), screen.y(), screen.width(), 120)
        layout = W.QVBoxLayout(self.root)
        layout.setContentsMargins(16, 5, 16, 5)
        top = W.QGridLayout()
        for column in range(6):
            top.setColumnStretch(column, 1)
        self.metrics = {}
        for key, column, span in (('hr', 0, 1), ('cadence', 1, 1), ('power', 2, 2), ('speed', 4, 1), ('dist', 5, 1)):
            label = W.QLabel('—')
            label.setAlignment(C.Qt.AlignCenter)
            label.setSizePolicy(W.QSizePolicy.Ignored, W.QSizePolicy.Preferred)
            shadow = W.QGraphicsDropShadowEffect(label)
            shadow.setBlurRadius(6)
            shadow.setOffset(0, 1)
            shadow.setColor(G.QColor('#000000'))
            label.setGraphicsEffect(shadow)
            top.addWidget(label, 0, column, 1, span)
            self.metrics[key] = label
        layout.addLayout(top)
        bottom = W.QHBoxLayout()
        self.details = W.QLabel('A ligar aos dispositivos…')
        self.details.setTextFormat(C.Qt.PlainText)
        self.details.setMinimumWidth(0)
        self.details.setSizePolicy(W.QSizePolicy.Ignored, W.QSizePolicy.Preferred)
        self.details.setStyleSheet('font-size: 14px; color: #b6c6d9')
        bottom.addWidget(self.details, 1)
        for caption, action in (('− 5 W', lambda: self.adjust(-5)), ('+ 5 W', lambda: self.adjust(5)), ('Menu / Esc', self.open_menu)):
            button = W.QPushButton(caption)
            button.clicked.connect(action)
            bottom.addWidget(button)
        layout.addLayout(bottom)
        self.profile = create_profile(C, G, W, self.root)
        self.profile.hide()
        layout.addWidget(self.profile)
        # Reduzir a tipografia em ecrãs pequenos sem perder a largura total.
        self.font_size = max(16, min(32, screen.width() // 48))
        self.root.show()
        self.pump()

    def adjust(self, delta):
        self.controls['delta'] += delta

    def open_menu(self):
        _, _, W = qt()
        if self.menu:
            self.menu.raise_()
            return
        menu = W.QDialog(self.root)
        menu.setStyleSheet('QDialog { background: #101720; color: #eeeeee; }')
        menu.setWindowTitle('Treino')
        menu.setWindowFlag(qt()[0].Qt.WindowStaysOnTopHint)
        layout = W.QVBoxLayout(menu)
        for caption, action in (
            ('Continuar treino', lambda: self.controls.update(paused=False)),
            ('Pausar treino', lambda: self.controls.update(paused=True)),
            ('Terminar e guardar', lambda: self.controls.update(stop=True)),
        ):
            button = W.QPushButton(caption)
            def clicked(checked=False, action=action):
                action()
                menu.accept()
            button.clicked.connect(clicked)
            layout.addWidget(button)
        self.menu = menu
        menu.finished.connect(lambda _: setattr(self, 'menu', None))
        menu.show()

    def show(self, s):
        if self.root is None:
            return
        def number(value, fmt='.0f'):
            return '—' if value is None else format(value, fmt)
        values = {
            'hr': ('PULSAÇÃO', number(s.get('hr')) + ' bpm'),
            'cadence': ('CADÊNCIA', number(s.get('cadence')) + ' rpm'),
            'speed': ('VELOCIDADE', number(s.get('speed'), '.1f') + ' km/h'),
            'dist': ('DISTÂNCIA', number(s.get('dist', 0) / 1000, '.2f') + ' km'),
        }
        for key, (title, value) in values.items():
            self.metrics[key].setText(f'<span style="font-size:10px;color:#a8b8ca">{title}</span><br><span style="font-size:{self.font_size}px;font-weight:bold;color:#eeeeee">{value}</span>')
        color = power_color(s.get('power3'), s.get('target'))
        target = number(s['target']) + ' W' if s.get('target') is not None else 'livre'
        self.metrics['power'].setText(
            '<span style="font-size:10px;color:#ffad33">POTÊNCIA · 3 s / OBJETIVO</span><br>'
            f'<span style="font-size:{self.font_size + 4}px;font-weight:bold;color:{color}">{number(s.get("power3"))}</span>'
            f'<span style="font-size:{self.font_size}px;font-weight:bold;color:#ff9f1c"> / {target}</span>')
        profile = s.get('profile', [])
        self.profile.set_session(profile, s['elapsed'])
        self.root.setFixedHeight(160 if profile else 120)
        status = 'PAUSA MANUAL' if self.controls['paused'] else 'PAUSA · pedala para continuar' if s['paused'] else s['name']
        remaining = mmss(s['remaining']) if s['remaining'] is not None else 'sem limite'
        total_left = mmss(max(0, s['total'] - s['elapsed'])) if s['total'] is not None else 'sem limite'
        next_step = f"{s.get('next_name', '')} · {number(s['next_watts'])} W · {mmss(s['next_dur'])}" if s.get('next_name') else '—'
        self.details.setText(f"{status}  |  {s['idx']}/{s['n']}  |  Tempo {mmss(s['elapsed'])}  |  Restante {total_left}  |  Bloco {remaining}  |  Próximo: {next_step}")
        self.details.setToolTip(self.details.text())
        self.root.update()

    def close(self):
        if self.root is not None:
            if self.menu:
                self.menu.reject()
            self.root.hide()
            self.root.deleteLater()
            self.root = None
            self.app.processEvents()

    def pump(self):
        self.app.processEvents()
