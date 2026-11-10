"""XLISP Studio: a native Tk editor and asynchronous console for real XLISP."""
from __future__ import annotations
import codecs
import json
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get('APPDATA', str(Path.home() / '.config'))) / 'XLISP-Studio'
BG, PANEL, FIELD, FG, MUTED, BLUE = '#10141d', '#181f2b', '#111722', '#e5eaf4', '#8e9bb1', '#79a8ff'
DEFAULT = '''; XLISP Studio — Ctrl+Enter запускает код или выделение.
; Определения сохраняются до перезапуска интерпретатора.

(define (square x)
  (* x x))

(square 12)
(map square '(1 2 3 4 5))
'''

class Engine:
    """Own a process; only background threads perform blocking pipe I/O."""
    def __init__(self, events, generation):
        binary = ROOT / 'engine' / ('xlisp.exe' if os.name == 'nt' else 'xlisp')
        if not binary.exists():
            raise FileNotFoundError(f'Не найден интерпретатор: {binary}')
        options = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
        env = os.environ.copy()
        env.pop('XLPATH', None)  # Always use the bundled support scripts.
        self.proc = subprocess.Popen([str(binary)], cwd=ROOT / 'engine', env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0, **options)
        self.events, self.generation = events, generation
        self.commands = queue.Queue(maxsize=64)
        self.closed = threading.Event()
        threading.Thread(target=self._reader, daemon=True).start()
        threading.Thread(target=self._writer, daemon=True).start()

    def _reader(self):
        decoder = codecs.getincrementaldecoder('utf-8')('replace')
        try:
            while not self.closed.is_set():
                chunk = self.proc.stdout.read(4096)
                if not chunk:
                    break
                self.events.put((self.generation, 'output', decoder.decode(chunk)))
            tail = decoder.decode(b'', final=True)
            if tail:
                self.events.put((self.generation, 'output', tail))
        except (OSError, ValueError):
            pass
        finally:
            self.events.put((self.generation, 'exit', self.proc.wait()))

    def _writer(self):
        try:
            while not self.closed.is_set():
                data = self.commands.get()
                if data is None:
                    return
                view = memoryview(data)
                while view and not self.closed.is_set():
                    n = self.proc.stdin.write(view)
                    if not n:
                        raise BrokenPipeError('stdin closed')
                    view = view[n:]
        except (BrokenPipeError, OSError, ValueError) as exc:
            if not self.closed.is_set():
                self.events.put((self.generation, 'error', str(exc)))

    def send(self, text):
        if self.closed.is_set() or self.proc.poll() is not None:
            raise RuntimeError('Интерпретатор остановлен. Нажми «Перезапуск».')
        try:
            self.commands.put_nowait((text.rstrip('\r\n') + '\n').encode('utf-8'))
        except queue.Full:
            raise RuntimeError('Очередь заполнена. Дождись выполнения или останови программу.')

    def stop(self):
        self.closed.set()
        if self.proc.poll() is None:
            self.proc.kill()
        try:
            self.commands.put_nowait(None)
        except queue.Full:
            pass

class Studio:
    def __init__(self, root):
        self.root = root
        root.title('XLISP Studio')
        root.geometry('1120x820')
        root.minsize(820, 620)
        root.configure(bg=BG)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(1, weight=1)
        self.font_size = 12
        self.path = None
        self.dirty = False
        self.events = queue.Queue(maxsize=256)
        self.generation = 0
        self.engine = None
        self.history = []
        self.hindex = 0
        self.draft = ''
        self.state = tk.StringVar(value='Запуск…')
        self.follow = tk.BooleanVar(value=True)
        self.file_label = tk.StringVar(value='Новый файл')
        self.position = tk.StringVar(value='Строка 1 · Столбец 1')
        self._load_history()
        self._style()
        self._build()
        self._bindings()
        self.editor.insert('1.0', DEFAULT)
        self.editor.edit_modified(False)
        self.editor.edit_reset()
        self.editor.focus_set()
        self._highlight_job = None
        root.protocol('WM_DELETE_WINDOW', self.close)
        root.after(50, self.poll)
        self.start()
        root.after(200, self.highlight)

    def _style(self):
        style = ttk.Style()
        style.theme_use('clam')
        style.configure('TFrame', background=BG)
        style.configure('TButton', background=PANEL, foreground=FG, padding=(11, 8), borderwidth=0)
        style.map('TButton', background=[('active', '#2b3c59')])
        style.configure('Run.TButton', background='#325bcc', foreground='white')
        style.map('Run.TButton', background=[('active', '#416fe2')])
        style.configure('TCheckbutton', background=BG, foreground=MUTED)
        style.map('TCheckbutton', background=[('active', BG)], foreground=[('active', FG)])
        style.configure('TScrollbar', background='#344058', troughcolor=FIELD, borderwidth=0, arrowsize=13)
        style.configure('TPanedwindow', background='#29354a')

    def label(self, parent, text=None, **kw):
        return tk.Label(parent, text=text, bg=BG, fg=MUTED, font=('Segoe UI', 10), **kw)

    def _build(self):
        self.menu = tk.Menu(self.root)
        file = tk.Menu(self.menu, tearoff=False)
        for label, command, accel in [('Новый', self.new_file, 'Ctrl+N'), ('Открыть…', self.open_file, 'Ctrl+O'),
                ('Сохранить', self.save_file, 'Ctrl+S'), ('Сохранить как…', lambda: self.save_file(True), 'Ctrl+Shift+S'),
                ('Сохранить вывод…', self.save_output, '')]:
            file.add_command(label=label, command=command, accelerator=accel)
        self.menu.add_cascade(label='Файл', menu=file)
        edit = tk.Menu(self.menu, tearoff=False)
        for label, event, accel in [('Отменить', '<<Undo>>', 'Ctrl+Z'), ('Повторить', '<<Redo>>', 'Ctrl+Y'),
                ('Вырезать', '<<Cut>>', 'Ctrl+X'), ('Копировать', '<<Copy>>', 'Ctrl+C'),
                ('Вставить', '<<Paste>>', 'Ctrl+V'), ('Выделить всё', '<<SelectAll>>', 'Ctrl+A')]:
            edit.add_command(label=label, accelerator=accel, command=lambda e=event: self.edit_event(e))
        self.menu.add_cascade(label='Правка', menu=edit)
        session = tk.Menu(self.menu, tearoff=False)
        session.add_command(label='Запустить код / выделение', command=self.run_editor, accelerator='Ctrl+Enter')
        session.add_command(label='Выйти из отладчика', command=lambda: self.send('(reset)'))
        session.add_command(label='Остановить выполнение', command=self.stop, accelerator='F8')
        session.add_command(label='Перезапустить XLISP', command=self.restart, accelerator='F5')
        session.add_command(label='Очистить вывод', command=self.clear_output)
        self.menu.add_cascade(label='Сеанс', menu=session)
        self.menu.add_command(label='Помощь', command=self.help)
        self.root.config(menu=self.menu)
        top = ttk.Frame(self.root, padding=(18, 14))
        top.grid(row=0, column=0, sticky='ew')
        tk.Label(top, text='XLISP', bg=BG, fg=FG, font=('Segoe UI', 20, 'bold')).pack(side='left')
        self.label(top, ' STUDIO  /  рабочая среда').pack(side='left', padx=(0, 20))
        ttk.Button(top, text='▶ Запустить   Ctrl+Enter', style='Run.TButton', command=self.run_editor).pack(side='right')
        ttk.Button(top, text='Перезапуск', command=self.restart).pack(side='right', padx=6)
        ttk.Button(top, text='■ Стоп', command=self.stop).pack(side='right')
        panes = ttk.Panedwindow(self.root, orient='vertical')
        panes.grid(row=1, column=0, sticky='nsew', padx=18)
        upper = ttk.Frame(panes)
        lower = ttk.Frame(panes)
        panes.add(upper, weight=3)
        panes.add(lower, weight=2)
        bar = ttk.Frame(upper, padding=(0, 5, 0, 8))
        bar.pack(fill='x')
        self.label(bar, 'РЕДАКТОР').pack(side='left')
        self.label(bar, textvariable=self.file_label).pack(side='left', padx=18)
        ttk.Button(bar, text='Открыть', command=self.open_file).pack(side='right')
        ttk.Button(bar, text='Сохранить', command=self.save_file).pack(side='right', padx=6)
        editor_frame = ttk.Frame(upper)
        editor_frame.pack(fill='both', expand=True)
        self.gutter = tk.Canvas(editor_frame, width=48, bg=PANEL, highlightthickness=0)
        self.gutter.pack(side='left', fill='y')
        self.editor = self.text_widget(editor_frame, undo=True)
        self.editor.pack(side='left', fill='both', expand=True)
        sy = ttk.Scrollbar(editor_frame, command=self.editor.yview)
        sy.pack(side='right', fill='y')
        self.editor.config(yscrollcommand=lambda a, b: (sy.set(a, b), self.draw_lines()))
        sx = ttk.Scrollbar(upper, orient='horizontal', command=self.editor.xview)
        sx.pack(fill='x')
        self.editor.config(xscrollcommand=sx.set)
        for tag, color in [('comment', '#7f93a2'), ('string', '#b2d995'), ('keyword', '#a9a1ff'), ('number', '#f1bd86')]:
            self.editor.tag_configure(tag, foreground=color)
        outbar = ttk.Frame(lower, padding=(0, 10, 0, 8))
        outbar.pack(fill='x')
        self.label(outbar, 'КОНСОЛЬ').pack(side='left')
        ttk.Checkbutton(outbar, text='Следить за выводом', variable=self.follow).pack(side='left', padx=18)
        ttk.Button(outbar, text='↓ В конец', command=self.to_bottom).pack(side='right')
        ttk.Button(outbar, text='Очистить', command=self.clear_output).pack(side='right', padx=6)
        outframe = ttk.Frame(lower)
        outframe.pack(fill='both', expand=True)
        self.output = self.text_widget(outframe, state='disabled', height=10, wrap='word')
        self.output.pack(side='left', fill='both', expand=True)
        sb = ttk.Scrollbar(outframe, command=self.output.yview)
        sb.pack(side='right', fill='y')
        self.output.config(yscrollcommand=sb.set)
        self.output.tag_configure('input', foreground=BLUE)
        self.output.tag_configure('system', foreground='#d9b474')
        self.label(self.root, 'КОМАНДА   ·   Enter — отправить   ·   Shift+Enter — новая строка   ·   Alt+↑/↓ — история', anchor='w').grid(row=2, column=0, sticky='ew', padx=18, pady=(10, 5))
        bottom = ttk.Frame(self.root)
        bottom.grid(row=3, column=0, sticky='ew', padx=18)
        self.command = self.text_widget(bottom, height=3, undo=True, wrap='word')
        self.command.pack(side='left', fill='both', expand=True)
        ttk.Button(bottom, text='Отправить ↵', command=self.run_command).pack(side='right', padx=(10, 0), fill='y')
        status = ttk.Frame(self.root, padding=(18, 8))
        status.grid(row=4, column=0, sticky='ew')
        self.label(status, textvariable=self.state).pack(side='left')
        self.label(status, textvariable=self.position).pack(side='right')
        for widget in (self.editor, self.command, self.output):
            self.context_menu(widget)

    def text_widget(self, parent, **kw):
        defaults = dict(bg=FIELD, fg=FG, insertbackground=BLUE, selectbackground='#344f7b',
                        selectforeground='white', font=('Consolas', self.font_size), padx=12, pady=10,
                        wrap='none', relief='flat', borderwidth=0, highlightthickness=1,
                        highlightbackground='#263148', highlightcolor=BLUE, exportselection=False)
        defaults.update(kw)
        return tk.Text(parent, **defaults)

    def _bindings(self):
        for seq, action in [('<Control-Return>', self.run_editor), ('<Control-o>', self.open_file),
                ('<Control-s>', self.save_file), ('<Control-Shift-S>', lambda: self.save_file(True)),
                ('<Control-n>', self.new_file), ('<F5>', self.restart), ('<F8>', self.stop)]:
            self.root.bind(seq, lambda e, fn=action: self.invoke(fn))
        for widget in (self.editor, self.command, self.output):
            # Physical keycodes keep the standard shortcuts working with a Russian layout on Windows.
            widget.bind('<Control-KeyPress>', self.ctrl_key)
        self.editor.bind('<<Modified>>', self.modified)
        self.editor.bind('<KeyRelease>', lambda e: self.cursor())
        self.editor.bind('<ButtonRelease-1>', lambda e: self.cursor())
        self.editor.bind('<Configure>', lambda e: self.draw_lines())
        self.editor.bind('<Tab>', lambda e: self.insert_indent())
        self.command.bind('<Control-Return>', lambda e: self.invoke(self.run_command))
        self.command.bind('<Return>', lambda e: self.invoke(self.run_command))
        self.command.bind('<Shift-Return>', lambda e: None)
        self.command.bind('<Up>', lambda e: self.history_key(-1, e))
        self.command.bind('<Down>', lambda e: self.history_key(1, e))
        self.command.bind('<Alt-Up>', lambda e: self.history_move(-1))
        self.command.bind('<Alt-Down>', lambda e: self.history_move(1))

    @staticmethod
    def invoke(fn):
        fn()
        return 'break'

    def ctrl_key(self, event):
        keys = {65: 'a', 67: 'c', 86: 'v', 88: 'x', 90: 'z', 89: 'y', 83: 's', 79: 'o', 78: 'n'}
        key = keys.get(event.keycode) if os.name == 'nt' else event.keysym.lower()
        actions = {'a': '<<SelectAll>>', 'c': '<<Copy>>', 'v': '<<Paste>>', 'x': '<<Cut>>', 'z': '<<Undo>>', 'y': '<<Redo>>'}
        if key in actions:
            event.widget.event_generate(actions[key])
            return 'break'
        if key in ('s', 'o', 'n'):
            action = {'s': lambda: self.save_file(bool(event.state & 1)), 'o': self.open_file, 'n': self.new_file}[key]
            return self.invoke(action)

    def context_menu(self, widget):
        menu = tk.Menu(widget, tearoff=False)
        for label, event in [('Копировать', '<<Copy>>'), ('Вставить', '<<Paste>>'), ('Вырезать', '<<Cut>>'), ('Выделить всё', '<<SelectAll>>')]:
            if widget is self.output and event in ('<<Paste>>', '<<Cut>>'):
                continue
            menu.add_command(label=label, command=lambda e=event: widget.event_generate(e))
        def show(e):
            widget.focus_set()
            try:
                menu.tk_popup(e.x_root, e.y_root)
            finally:
                menu.grab_release()
        widget.bind('<Button-3>', show)

    def edit_event(self, event):
        widget = self.root.focus_get()
        if isinstance(widget, tk.Text):
            widget.event_generate(event)

    def insert_indent(self):
        self.editor.insert('insert', '  ')
        return 'break'

    def cursor(self):
        row, col = map(int, self.editor.index('insert').split('.'))
        self.position.set(f'Строка {row} · Столбец {col + 1}')

    def draw_lines(self):
        if not hasattr(self, 'editor'):
            return
        self.gutter.delete('all')
        index = self.editor.index('@0,0')
        while True:
            info = self.editor.dlineinfo(index)
            if info is None:
                break
            self.gutter.create_text(38, info[1], anchor='ne', text=index.split('.')[0],
                fill=MUTED, font=('Consolas', self.font_size))
            index = self.editor.index(index + '+1line')

    def modified(self, event=None):
        if self.editor.edit_modified():
            self.dirty = True
            self.update_title()
            if getattr(self, '_highlight_job', None):
                self.root.after_cancel(self._highlight_job)
            self._highlight_job = self.root.after(180, self.highlight)
            self.editor.edit_modified(False)
        self.draw_lines()

    def highlight(self):
        self._highlight_job = None
        text = self.editor.get('1.0', 'end-1c')
        for tag in ('comment', 'string', 'keyword', 'number'):
            self.editor.tag_remove(tag, '1.0', 'end')
        # Limit decoration work for large files; execution and editing stay available.
        pattern = r';[^\n]*|"(?:\\.|[^"\\])*"|(?<![\w-])(?:define|lambda|if|let\*?|set!|cond|begin|quote|defun|progn|map|car|cdr|cons|list|format|load)(?![\w!?-])|(?<![\w-])-?\d+(?:\.\d+)?(?![\w-])'
        for m in re.finditer(pattern, text[:200000]):
            token = m.group()
            tag = 'comment' if token.startswith(';') else 'string' if token.startswith('"') else 'number' if re.fullmatch(r'-?\d+(\.\d+)?', token) else 'keyword'
            self.editor.tag_add(tag, f'1.0+{m.start()}c', f'1.0+{m.end()}c')
        self.cursor()
        self.draw_lines()

    def update_title(self):
        name = self.path.name if self.path else 'Новый файл'
        self.file_label.set(name + (' •' if self.dirty else ''))
        self.root.title(f'{name}{" *" if self.dirty else ""} — XLISP Studio')

    def append(self, text, tag=None):
        at_bottom = self.output.yview()[1] >= .995
        selection = self.output.tag_ranges('sel')
        cursor = self.output.index('insert')
        self.output.config(state='normal')
        self.output.insert('end', text, (tag,) if tag else ())
        self.output.mark_set('insert', cursor)
        if selection:
            self.output.tag_add('sel', *selection)
        self.output.config(state='disabled')
        if self.follow.get() and at_bottom:
            self.output.see('end')

    def poll(self):
        # Bounded work per frame, so endless print loops cannot starve the UI.
        for _ in range(80):
            try:
                generation, kind, data = self.events.get_nowait()
            except queue.Empty:
                break
            if generation != self.generation:
                continue
            if kind == 'output':
                self.append(data)
            elif kind == 'exit':
                self.state.set(f'XLISP остановлен · код {data} · F5 для запуска')
            else:
                self.append(f'\n{data}\n', 'system')
        self.root.after(40, self.poll)

    def start(self):
        self.generation += 1
        try:
            self.engine = Engine(self.events, self.generation)
            self.state.set('XLISP запущен · сеанс сохраняет определения · F8 — остановить')
        except (OSError, RuntimeError) as exc:
            self.state.set('Не удалось запустить XLISP')
            messagebox.showerror('XLISP', str(exc))

    def stop(self):
        if self.engine:
            self.engine.stop()
        self.state.set('Остановлен · F5 — новый сеанс')

    def restart(self):
        if self.engine and self.engine.proc.poll() is None:
            if not messagebox.askyesno('Новый сеанс', 'Перезапустить XLISP? Определения и переменные текущего сеанса будут потеряны. Код в редакторе сохранится.'):
                return
        self.stop()
        self.append('\n── Новый сеанс ──\n', 'system')
        self.start()

    def send(self, code):
        if not code.strip():
            return False
        try:
            if not self.engine:
                raise RuntimeError('Нажми «Перезапуск».')
            self.engine.send(code)
        except (OSError, RuntimeError) as exc:
            messagebox.showerror('Отправка команды', str(exc))
            return False
        self.append(code.rstrip() + '\n', 'input')
        if not self.history or self.history[-1] != code:
            self.history.append(code)
            self.history = self.history[-300:]
        self.hindex = len(self.history)
        self._save_history()
        return True

    def run_editor(self):
        try:
            code = self.editor.get('sel.first', 'sel.last')
        except tk.TclError:
            code = self.editor.get('1.0', 'end-1c')
        self.send(code)

    def run_command(self):
        if self.send(self.command.get('1.0', 'end-1c')):
            self.command.delete('1.0', 'end')
            self.draft = ''
        return 'break'

    def history_key(self, delta, event):
        row = int(self.command.index('insert').split('.')[0])
        last = int(self.command.index('end-1c').split('.')[0])
        if (delta < 0 and row == 1) or (delta > 0 and row == last):
            return self.history_move(delta)
        return None

    def history_move(self, delta):
        if self.hindex == len(self.history):
            self.draft = self.command.get('1.0', 'end-1c')
        self.hindex = max(0, min(len(self.history), self.hindex + delta))
        value = self.history[self.hindex] if self.hindex < len(self.history) else self.draft
        self.command.delete('1.0', 'end')
        self.command.insert('1.0', value)
        self.command.mark_set('insert', 'end-1c')
        return 'break'

    def _load_history(self):
        try:
            values = json.loads((DATA / 'history.json').read_text(encoding='utf-8'))
            self.history = [x for x in values if isinstance(x, str)][-300:]
        except (OSError, ValueError, TypeError):
            pass
        self.hindex = len(self.history)

    def _save_history(self):
        try:
            DATA.mkdir(parents=True, exist_ok=True)
            tmp = DATA / 'history.tmp'
            tmp.write_text(json.dumps(self.history, ensure_ascii=False), encoding='utf-8')
            tmp.replace(DATA / 'history.json')
        except OSError:
            self.state.set('Не удалось сохранить историю на диск')

    def confirm_dirty(self):
        if not self.dirty:
            return True
        answer = messagebox.askyesnocancel('Изменения в редакторе', 'Сохранить изменения в файле?')
        if answer is None:
            return False
        return self.save_file() if answer else True

    def new_file(self):
        if not self.confirm_dirty():
            return
        self.editor.delete('1.0', 'end')
        self.editor.edit_reset()
        self.editor.edit_modified(False)
        self.path, self.dirty = None, False
        self.update_title()

    def open_file(self):
        if not self.confirm_dirty():
            return
        name = filedialog.askopenfilename(initialdir=str(ROOT / 'examples'), filetypes=[('Lisp', '*.lsp *.lisp *.scm'), ('Все файлы', '*.*')])
        if not name:
            return
        try:
            content = Path(name).read_text(encoding='utf-8-sig')
        except (OSError, UnicodeError) as exc:
            messagebox.showerror('Открытие файла', f'Нужен текстовый файл в UTF-8.\n{exc}')
            return
        self.editor.delete('1.0', 'end')
        self.editor.insert('1.0', content)
        self.editor.edit_reset()
        self.editor.edit_modified(False)
        self.path, self.dirty = Path(name), False
        self.update_title()
        self.highlight()

    def save_file(self, save_as=False):
        path = self.path
        if path is None or save_as:
            name = filedialog.asksaveasfilename(defaultextension='.lsp', initialfile=path.name if path else 'program.lsp', filetypes=[('Lisp', '*.lsp'), ('Все файлы', '*.*')])
            if not name:
                return False
            path = Path(name)
        try:
            path.write_text(self.editor.get('1.0', 'end-1c'), encoding='utf-8')
        except OSError as exc:
            messagebox.showerror('Сохранение файла', str(exc))
            return False
        self.path, self.dirty = path, False
        self.update_title()
        return True

    def save_output(self):
        name = filedialog.asksaveasfilename(defaultextension='.txt', initialfile='xlisp-session.txt')
        if name:
            try:
                Path(name).write_text(self.output.get('1.0', 'end-1c'), encoding='utf-8')
            except OSError as exc:
                messagebox.showerror('Сохранение вывода', str(exc))

    def clear_output(self):
        self.output.config(state='normal')
        self.output.delete('1.0', 'end')
        self.output.config(state='disabled')

    def to_bottom(self):
        self.follow.set(True)
        self.output.see('end')

    def help(self):
        messagebox.showinfo('XLISP Studio',
            'Редактор: Ctrl+Enter — запустить выделение или весь код.\n'
            'Консоль: колесо мыши / PageUp / PageDown — прокрутка.\n'
            'Ctrl+C / Ctrl+V / Ctrl+X / Ctrl+A и меню правой кнопки.\n'
            'Команда: Enter — отправить; Shift+Enter — новая строка.\n'
            '↑/↓ на границах ввода или Alt+↑/↓ — история команд.\n'
            'Ctrl+Z / Ctrl+Y — отмена и повтор в редакторе.\n'
            'Ctrl+O / Ctrl+S — открыть / сохранить .lsp в UTF-8.\n'
            'F8 — остановить; F5 — новый сеанс.\n'
            'После ошибки XLISP может войти в Debug: введи (reset).\n\n'
            'Движок: оригинальный dbetz/xlisp 3.3. Это его диалект,\n'
            'поэтому код других Lisp может требовать изменений.\n'
            'Рабочая папка интерпретатора — engine/. Для (load)\n'
            'снаружи указывай полный путь с прямыми слешами.\n'
            'Вставленный текст выполняется только после запуска.')

    def close(self):
        if not self.confirm_dirty():
            return
        self._save_history()
        self.stop()
        self.root.destroy()

if __name__ == '__main__':
    root = tk.Tk()
    app = Studio(root)
    root.mainloop()
