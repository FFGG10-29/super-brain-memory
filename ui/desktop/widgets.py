"""ui.desktop.widgets —— 通用卡片、KPI、迷你地图等复用控件。"""
from __future__ import annotations

from PySide6 import QtCore, QtGui, QtWidgets

__all__ = ["Card", "KpiCard", "MiniMap", "divider"]


def divider() -> QtWidgets.QFrame:
    line = QtWidgets.QFrame()
    line.setObjectName("divider")
    line.setFixedHeight(1)
    return line


class Card(QtWidgets.QFrame):
    """标题 + 内容区的圆角面板。内容请加到 `card.body`。"""

    def __init__(self, title: str = "", parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("card")
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 14)
        layout.setSpacing(10)
        if title:
            label = QtWidgets.QLabel(title)
            label.setObjectName("cardTitle")
            layout.addWidget(label)
        self.body = QtWidgets.QVBoxLayout()
        self.body.setSpacing(8)
        layout.addLayout(self.body)


class KpiCard(QtWidgets.QFrame):
    def __init__(self, caption: str, value: str = "—", parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("card")
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 12)
        layout.setSpacing(2)
        self._value = QtWidgets.QLabel(value)
        self._value.setObjectName("kpiValue")
        self._caption = QtWidgets.QLabel(caption)
        self._caption.setObjectName("kpiCaption")
        layout.addWidget(self._value)
        layout.addWidget(self._caption)

    def set_value(self, value: object) -> None:
        self._value.setText(str(value))

    def set_accent(self, color: str) -> None:
        self._value.setStyleSheet(f"color: {color};")


class MiniMap(QtWidgets.QGraphicsView):
    """右下角缩略图：共享主场景 + 视口矩形指示。"""

    def __init__(self, scene: QtWidgets.QGraphicsScene, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(scene, parent)
        self.setFixedSize(190, 126)
        self.setInteractive(False)
        self.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, False)
        for policy in (QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff,):
            self.setHorizontalScrollBarPolicy(policy)
            self.setVerticalScrollBarPolicy(policy)
        self.setStyleSheet(
            "background-color: rgba(7, 10, 16, 205); border: 1px solid #1E2B3F; border-radius: 8px;"
        )
        self._area = QtCore.QRectF()

    def refresh(self) -> None:
        self.fitInView(self.sceneRect(), QtCore.Qt.AspectRatioMode.KeepAspectRatio)
        self.viewport().update()

    def set_view_area(self, rect: QtCore.QRectF) -> None:
        self._area = QtCore.QRectF(rect)
        self.viewport().update()

    def showEvent(self, event: QtGui.QShowEvent) -> None:  # noqa: N802
        super().showEvent(event)
        self.refresh()

    def drawForeground(self, painter: QtGui.QPainter, rect: QtCore.QRectF) -> None:  # noqa: N802
        if self._area.isEmpty():
            return
        painter.setPen(QtGui.QPen(QtGui.QColor(79, 195, 247, 170), 1))
        painter.setBrush(QtGui.QBrush(QtGui.QColor(79, 195, 247, 18)))
        painter.drawRect(self._area)
