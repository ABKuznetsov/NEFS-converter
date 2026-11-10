"""Small Tk interface. Conversion runs outside the UI thread."""

import queue
import threading
from dataclasses import dataclass
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from .cli import _output_path, convert_source


@dataclass(frozen=True)
class ConversionRequest:
    source: Path
    output: Path


def conversion_request(source: str, output: str) -> ConversionRequest:
    if not source.strip() or not output.strip():
        raise ValueError('Select the SEM file and the NESF output file.')
    source_path = Path(source).expanduser().resolve()
    if not source_path.is_file():
        raise FileNotFoundError(f'SEM file not found: {source_path}')
    if not (source_path.parent / 'data').is_dir():
        raise FileNotFoundError('The original data/ directory must remain beside the SEM file.')
    return ConversionRequest(source_path, _output_path(source_path.parent, Path(output).expanduser()))


class ConverterWindow:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.events = queue.Queue()
        self.busy = False
        self.source = tk.StringVar()
        self.output = tk.StringVar()
        self.status = tk.StringVar(value='Ready')
        root.title('SEM Data Converter')
        root.geometry('760x430')
        root.minsize(600, 380)
        root.protocol('WM_DELETE_WINDOW', self.close)
        body = ttk.Frame(root, padding=20)
        body.pack(fill='both', expand=True)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(5, weight=1)
        ttk.Label(body, text='SEM Data Converter', font=('Segoe UI', 16, 'bold')).grid(row=0, column=0, columnspan=3, sticky='w', pady=(0, 20))
        ttk.Label(body, text='SEM file').grid(row=1, column=0, sticky='w', padx=(0, 12))
        source_entry = ttk.Entry(body, textvariable=self.source)
        source_entry.grid(row=1, column=1, sticky='ew', pady=6)
        source_browse = ttk.Button(body, text='Browse...', command=self.browse_source)
        source_browse.grid(row=1, column=2, padx=(8, 0))
        ttk.Label(body, text='Save NESF as').grid(row=2, column=0, sticky='w', padx=(0, 12))
        output_entry = ttk.Entry(body, textvariable=self.output)
        output_entry.grid(row=2, column=1, sticky='ew', pady=6)
        output_browse = ttk.Button(body, text='Browse...', command=self.browse_output)
        output_browse.grid(row=2, column=2, padx=(8, 0))
        controls = ttk.Frame(body)
        controls.grid(row=3, column=0, columnspan=3, sticky='ew', pady=(16, 8))
        controls.columnconfigure(0, weight=1)
        ttk.Label(controls, textvariable=self.status).grid(row=0, column=0, sticky='w')
        self.start_button = ttk.Button(controls, text='Create NESF', command=self.start)
        self.start_button.grid(row=0, column=1, sticky='e')
        self.progress = ttk.Progressbar(body, mode='indeterminate')
        self.progress.grid(row=4, column=0, columnspan=3, sticky='ew', pady=(0, 12))
        self.log = ScrolledText(body, height=8, wrap='word', font=('Consolas', 10), relief='solid', borderwidth=1)
        self.log.grid(row=5, column=0, columnspan=3, sticky='nsew')
        self.log.configure(state='disabled')
        self.inputs = [source_entry, output_entry, source_browse, output_browse, self.start_button]
        root.after(100, self.poll)

    def browse_source(self):
        path = filedialog.askopenfilename(parent=self.root, title='Select SEM file',
                                          filetypes=[('SEM data files', '*.oip'), ('All files', '*.*')])
        if path:
            self.source.set(path)
            self.output.set(str(Path(path).with_suffix('.nesf')))

    def browse_output(self):
        source = Path(self.source.get()) if self.source.get() else Path.home()
        path = filedialog.asksaveasfilename(parent=self.root, title='Save NESF as', defaultextension='.nesf',
                                           initialdir=str(source.parent if source.is_file() else source),
                                           initialfile=source.with_suffix('.nesf').name if source.is_file() else 'project.nesf',
                                           filetypes=[('NESF dataset', '*.nesf')])
        if path:
            self.output.set(path)

    def append_log(self, message):
        self.log.configure(state='normal')
        self.log.insert('end', str(message) + '\n')
        self.log.see('end')
        self.log.configure(state='disabled')

    def start(self):
        if self.busy:
            return
        try:
            request = conversion_request(self.source.get(), self.output.get())
        except (OSError, ValueError) as exc:
            messagebox.showerror('SEM Data Converter', str(exc), parent=self.root)
            return
        overwrite = request.output.exists()
        if overwrite and not messagebox.askyesno('Replace NESF?', f'Replace this file?\n{request.output}', parent=self.root):
            return
        self.busy = True
        for widget in self.inputs:
            widget.configure(state='disabled')
        self.status.set('Converting...')
        self.progress.start(12)
        threading.Thread(target=self.convert, args=(request, overwrite), daemon=False).start()

    def convert(self, request, overwrite):
        try:
            result = convert_source(request.source, request.output, overwrite=overwrite,
                                    progress=lambda text: self.events.put(('log', text)))
            self.events.put(('done', result))
        except Exception as exc:
            self.events.put(('error', str(exc)))

    def poll(self):
        while True:
            try:
                kind, payload = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == 'log':
                self.append_log(payload)
                continue
            self.busy = False
            self.progress.stop()
            for widget in self.inputs:
                widget.configure(state='normal')
            if kind == 'error':
                self.status.set('Conversion failed')
                self.append_log(payload)
                messagebox.showerror('SEM Data Converter', payload, parent=self.root)
            else:
                self.status.set('Saved with warnings' if payload['status'] == 'partial' else 'NESF saved')
                self.append_log(payload['output'])
                self.append_log('Original data unchanged. Calibration requires independent validation.')
                for diagnostic in payload['diagnostics']:
                    self.append_log(f"{diagnostic['kind']}: {diagnostic['reason']}")
        self.root.after(100, self.poll)

    def close(self):
        if self.busy:
            messagebox.showinfo('SEM Data Converter', 'Conversion is still running.', parent=self.root)
        else:
            self.root.destroy()


def main_gui() -> int:
    root = tk.Tk()
    ConverterWindow(root)
    root.mainloop()
    return 0
