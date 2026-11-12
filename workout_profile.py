"""Perfil em degraus: largura = duração, altura = potência objetivo."""


def create_profile(C, G, W, parent=None):
    class WorkoutProfile(W.QWidget):
        def __init__(self):
            super().__init__(parent)
            self.blocks = []
            self.elapsed = 0
            self.setFixedHeight(40)
            self.setToolTip('Perfil do treino: duração na horizontal, potência na vertical. A linha branca indica o ponto atual.')

        def set_session(self, blocks, elapsed):
            self.blocks, self.elapsed = blocks, elapsed
            self.setVisible(bool(blocks))
            self.update()

        def paintEvent(self, event):
            if not self.blocks:
                return
            painter = G.QPainter(self)
            painter.setRenderHint(G.QPainter.Antialiasing)
            total = sum(b['duration'] for b in self.blocks)
            peak = max((b['watts'] or 0 for b in self.blocks), default=0) or 1
            if total <= 0:
                return
            width, baseline = self.width() - 4, self.height() - 3
            position = 0
            path = G.QPainterPath()
            for index, block in enumerate(self.blocks):
                end = position + block['duration']
                x, right = 2 + width * position / total, 2 + width * end / total
                y = baseline - (5 + (baseline - 9) * (block['watts'] or 0) / peak)
                current = position <= self.elapsed < end
                fill = G.QColor('#ff9f1c')
                fill.setAlpha(160 if current else 45 if end <= self.elapsed else 80)
                painter.fillRect(C.QRectF(x, y, right - x, baseline - y), fill)
                if index == 0:
                    path.moveTo(x, y)
                else:
                    path.lineTo(x, y)
                path.lineTo(right, y)
                position = end
            painter.setPen(G.QPen(G.QColor('#ffad33'), 2))
            painter.drawPath(path)
            cursor = 2 + width * min(max(self.elapsed / total, 0), 1)
            painter.setPen(G.QPen(G.QColor('#ffffff'), 2))
            painter.drawLine(C.QPointF(cursor, 1), C.QPointF(cursor, baseline))
            painter.end()

    return WorkoutProfile()
