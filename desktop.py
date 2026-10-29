"""Two native windows, one tray icon and explicit application shutdown."""
import threading


class WidgetBridge:
    """Only geometry is exposed to the recording page."""
    def __init__(self, desktop):
        self._desktop = desktop

    def set_widget_compact(self, enabled):
        return self._desktop.set_compact(enabled is True)


class Desktop:
    def __init__(self, url, stop_capture, drain_capture, stop_analysis, stop_probe):
        import webview
        self.stop_capture, self.drain_capture = stop_capture, drain_capture
        self.stop_analysis, self.stop_probe = stop_analysis, stop_probe
        self.exiting = False
        self._compact = False
        self._expanded_size = None
        self._expanded_max = None
        self.widget = webview.create_window('Transcriptor', url, width=360, height=540,
                                           min_size=(300,360), resizable=True, frameless=True,
                                           easy_drag=False, background_color='#1c1f24', js_api=WidgetBridge(self))
        self.manager = webview.create_window('Transcriptor · Sesiones', url + '/manager', width=1100,
                                            height=760, min_size=(760,500), hidden=True,
                                            background_color='#171b22')
        # Event handlers must return a hashable value (bool/None), never the
        # dict returned by the capture API. pywebview collects results in a set.
        self.widget.events.closing += self.close_widget
        self.manager.events.closing += self.close_manager
        self.tray = None

    def set_compact(self, enabled):
        form = self.widget.native
        if form is None or self.exiting:
            return False
        from System import Action
        from System.Drawing import Size, Point
        from System.Windows.Forms import Screen
        def apply():
            if enabled == self._compact:
                return
            scale = form.DeviceDpi / 96.0
            if enabled:
                self._expanded_size = (form.Width / scale, form.Height / scale)
                self._expanded_max = form.MaximumSize
                form.MinimumSize = Size(0,0)
                form.MaximumSize = Size(0,0)
                target = Size(round(320*scale),round(64*scale))
                form.Size = target
                form.MinimumSize = target
                form.MaximumSize = target
            else:
                form.MinimumSize = Size(0,0)
                form.MaximumSize = self._expanded_max or Size(0,0)
                width,height = self._expanded_size or (360,540)
                form.Size = Size(round(max(300,width)*scale),round(max(360,height)*scale))
                form.MinimumSize = Size(round(300*scale),round(360*scale))
            area = Screen.FromControl(form).WorkingArea
            if not enabled:
                form.Size = Size(min(form.Width,max(round(300*scale),area.Width)),
                                 min(form.Height,max(round(360*scale),area.Height)))
            form.Location = Point(max(area.Left,min(form.Left,area.Right-form.Width)),
                                  max(area.Top,min(form.Top,area.Bottom-form.Height)))
            self._compact = enabled
        form.Invoke(Action(apply))
        return True

    def show_widget(self, *_):
        self.widget.show()
        self.widget.restore()

    def show_manager(self, *_):
        self.manager.show()
        self.manager.restore()

    def close_widget(self):
        if self.exiting:
            return True
        self.stop_capture()
        self.stop_probe()
        self.widget.hide()
        return False

    def close_manager(self):
        if self.exiting:
            return True
        self.stop_probe()
        self.manager.hide()
        return False

    def start_tray(self):
        import pystray
        from PIL import Image, ImageDraw
        image = Image.new('RGBA', (64,64), '#1c1f24')
        draw = ImageDraw.Draw(image)
        draw.ellipse((17,17,47,47), fill='#2bb3ff')
        self.tray = pystray.Icon('transcriptor', image, 'Transcriptor · Listo', pystray.Menu(
            pystray.MenuItem('Mostrar widget', self.show_widget, default=True),
            pystray.MenuItem('Abrir sesiones', self.show_manager),
            pystray.MenuItem('Salir', self.exit)))
        threading.Thread(target=self.tray.run, daemon=True).start()

    def activity(self, message):
        if not self.tray:
            return
        if message.get('type') == 'analysis':
            labels = {'pending':'Análisis pendiente', 'running':'Analizando', 'completed':'Análisis completado',
                      'error':'Error de análisis', 'interrupted':'Análisis interrumpido'}
            self.tray.title = 'Transcriptor · ' + labels.get(message.get('state'), 'Listo')
        elif message.get('type') == 'status' and message.get('state'):
            labels = {'recording':'Grabando', 'paused':'En pausa', 'finishing':'Terminando',
                      'starting':'Iniciando', 'finished':'Listo', 'ready':'Listo'}
            self.tray.title = 'Transcriptor · ' + labels.get(message['state'], 'Listo')

    def notify(self, state):
        if self.tray:
            try:
                self.tray.notify('Análisis completado' if state == 'completed' else 'No se pudo completar el análisis. Revisa Sesiones.', 'Transcriptor')
            except Exception:
                pass  # Windows may have notifications disabled.

    def exit(self, *_):
        if self.exiting:
            return
        self.exiting = True
        def shutdown():
            self.stop_analysis()
            self.stop_probe()
            self.stop_capture()
            self.drain_capture()
            self.stop_analysis()
            if self.tray:
                self.tray.stop()
            self.manager.destroy()
            self.widget.destroy()
        threading.Thread(target=shutdown, daemon=True).start()
