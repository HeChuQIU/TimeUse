import multiprocessing
import os
import subprocess
import xml.dom.minidom as minidom
import xml.etree.ElementTree as ET
from datetime import timedelta

import reactivex
import win32api
import win32con
import win32gui
import win32process
from reactivex import operators as ops
from reactivex.scheduler import ThreadPoolScheduler
from rich.text import Text
from textual.app import App, ComposeResult
from textual.reactive import Reactive, reactive
from textual.widgets import Header, Footer, Static, DataTable


def get_active_window_info():
    try:
        window = win32gui.GetForegroundWindow()
        t, p = win32process.GetWindowThreadProcessId(window)
        h_process = win32api.OpenProcess(win32con.PROCESS_QUERY_INFORMATION, False, p)
        process_name = win32process.GetModuleFileNameEx(h_process, 0)
        title = win32gui.GetWindowText(window)
    except Exception as e:
        return {"error": str(e), "process_id": -1, "process_name": "unknown", "title": "unknown"}

    return {"process_id": p, "process_name": process_name, "title": title}


class TimeRecord(dict):
    def add_timedelta(self, application: str, title: str, timedelta: timedelta):
        self[application] = self.get(application, {})
        self[application][title] = self[application].get(title, timedelta) + timedelta

    def to_xml(self):
        root = ET.Element("TimeRecord")
        for application, titles in self.items():
            app_element = ET.SubElement(root, "Application", name=application)
            for title, time in titles.items():
                title_element = ET.SubElement(app_element, "Title", name=title)
                title_element.text = str(time.total_seconds())
        return minidom.parseString(ET.tostring(root)).toprettyxml(indent="   ")

    @staticmethod
    def from_xml(xml: str):
        root = ET.fromstring(xml)
        record = TimeRecord()
        for app_element in root.findall("Application"):
            application = app_element.attrib["name"]
            record[application] = {}
            for title_element in app_element.findall("Title"):
                title = title_element.attrib["name"]
                time = timedelta(seconds=float(title_element.text))
                record[application][title] = time
        return record


class TimeUseApp(App):
    BINDINGS = [
        ("n", "switch_paused", "暂停/继续记录"),
        ("p", "sort_by_process", "按进程名排序"),
        ("t", "sort_by_time", "按使用时间排序"),
        ("o", "open_record", "打开记录文件所在目录"),
        ("q", "quit", "退出程序"),
    ]

    paused = reactive(False)
    data_table_title = ("应用程序", "标题", "使用时间")
    record = Reactive(TimeRecord())

    optimal_thread_count = multiprocessing.cpu_count()
    pool_scheduler = ThreadPoolScheduler(optimal_thread_count)

    sort_by_process_up = True
    sort_by_time_up = None

    def __init__(self):
        super().__init__()
        self.column_keys = None
        self.timer = reactivex.interval(1.0).pipe(ops.observe_on(self.pool_scheduler))
        self.saver_timer = reactivex.interval(10.0).pipe(ops.observe_on(self.pool_scheduler))

    def load_record(self) -> TimeRecord:
        try:
            with open("record.xml", "r", encoding="utf-8") as f:
                return TimeRecord.from_xml(f.read())
        except FileNotFoundError:
            return TimeRecord()

    @staticmethod
    def save_record(record: TimeRecord):
        with open("record.xml", "w", encoding="utf-8") as f:
            f.write(record.to_xml())

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static(id="paused")
        yield DataTable()
        yield Footer()

    def on_mount(self):
        table = self.query_one(DataTable)
        self.column_keys = table.add_columns(*self.data_table_title)

        self.record = self.load_record()

        paused_timer = self.timer.pipe(ops.filter(lambda _: not self.paused))
        paused_save_timer = self.saver_timer.pipe(ops.filter(lambda _: not self.paused))

        def record_and_print_error(x):
            self.record.add_timedelta(x["process_name"], x["title"], timedelta(seconds=1))
            if "error" in x:
                print(x["error"])

        info_subject = paused_timer.pipe(ops.map(lambda _: get_active_window_info()))
        info_subject.pipe(ops.filter(lambda info: "error" not in info)).subscribe(record_and_print_error)

        paused_save_timer.subscribe(lambda _: self.save_record(self.record))
        paused_save_timer.subscribe(lambda _: self.refresh_table())

        paused_save_timer.subscribe(lambda _: self.refresh_table())

        self.refresh_table()

    def watch_paused(self, old_paused: bool, new_paused: bool):
        if new_paused:
            self.sub_title = "已暂停"
            self.query_one(Header).styles.background = "red"
        else:
            self.sub_title = "记录中"
            self.query_one(Header).styles.background = "green"

    def refresh_table(self):
        def truncate_string(s: str, length: int = 20) -> str:
            return s[:length] + "..." if len(s) > length else s

        table = self.query_one(DataTable)
        coordinate = table.cursor_coordinate
        table.clear()
        data = self.record.items()
        if self.sort_by_process_up is not None:
            data = sorted(data, key=lambda x: x[0], reverse=self.sort_by_process_up)
        if self.sort_by_time_up is not None:
            data = sorted(data, key=lambda x: sum(x[1].values(), timedelta()), reverse=self.sort_by_time_up)
        for app, titles in data:
            row = [os.path.basename(app), "", sum(titles.values(), timedelta())]
            styled_row = [
                Text(str(cell), style="#03AC13") for cell in row
            ]
            table.add_row(*styled_row, key=app)

            td = titles.items()
            if self.sort_by_process_up is not None:
                td = sorted(td, key=lambda x: x[0], reverse=self.sort_by_process_up)
            if self.sort_by_time_up is not None:
                td = sorted(td, key=lambda x: x[1], reverse=self.sort_by_time_up)

            for i, (title, time) in enumerate(td):
                row = ["", truncate_string(title), time]
                table.add_row(*row, key=f"{app}_{title}")

            table.add_row()

        table.cursor_coordinate = coordinate

    def action_switch_paused(self):
        self.paused = not self.paused

    def action_open_record(self):
        record_file_path = os.path.abspath("record.xml")
        subprocess.Popen(f'explorer /select,"{record_file_path}"')

    def action_quit(self):
        self.app.exit()

    def action_sort_by_process(self):
        self.sort_by_time_up = None
        if self.sort_by_process_up is None:
            self.sort_by_process_up = True
        else:
            self.sort_by_process_up = not self.sort_by_process_up
        self.refresh_table()

    def action_sort_by_time(self):
        self.sort_by_process_up = None
        if self.sort_by_time_up is None:
            self.sort_by_time_up = True
        else:
            self.sort_by_time_up = not self.sort_by_time_up
        self.refresh_table()

    def _on_exit_app(self) -> None:
        self.save_record(self.record)
        super()._on_exit_app()


if __name__ == '__main__':
    app = TimeUseApp()
    app.run()
