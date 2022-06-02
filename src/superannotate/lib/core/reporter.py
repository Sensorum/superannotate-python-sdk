import itertools
import sys
import threading
import time
from collections import defaultdict
from typing import Union

import tqdm
from superannotate.logger import get_default_logger


class Spinner:
    spinner_cycle = iter(itertools.cycle(["⣾", "⣽", "⣻", "⢿", "⡿", "⣟", "⣯", "⣷"]))

    def __init__(self):
        self.stop_running = threading.Event()
        self.spin_thread = threading.Thread(target=self.init_spin)

    def start(self):
        self.spin_thread.start()

    def stop(self):
        self.stop_running.set()
        self.spin_thread.join()

    def init_spin(self):
        while not self.stop_running.is_set():
            sys.stdout.write(next(self.spinner_cycle))
            sys.stdout.flush()
            time.sleep(0.25)
            sys.stdout.write("\b")


class Session:
    def __init__(self):
        self.pk = threading.get_ident()
        self._data_dict = {}

    def __enter__(self):
        return self

    def __exit__(self, type, value, traceback):
        if type is not None:
            return False

    def __del__(self):
        globs = globals()
        # if "SESSIONS" in globs and globs.get("SESSIONS", {}).get(self.pk):
        #     del globs["SESSIONS"][self.pk]

    @property
    def data(self):
        return self._data_dict

    @staticmethod
    def get_current_session():
        globs = globals()
        if not globs.get("SESSIONS") or not globs["SESSIONS"].get(
            threading.get_ident()
        ):
            session = Session()
            globals().update({"SESSIONS": {session.pk: session}})
            return session
        return globs["SESSIONS"][threading.get_ident()]

    def __setitem__(self, key, item):
        self._data_dict[key] = item

    def __getitem__(self, key):
        return self._data_dict[key]

    def __repr__(self):
        return repr(self._data_dict)

    def clear(self):
        return self._data_dict.clear()


class Reporter:
    def __init__(
        self,
        log_info: bool = True,
        log_warning: bool = True,
        disable_progress_bar: bool = False,
        log_debug: bool = True,
        session: Session = None,
    ):
        self.logger = get_default_logger()
        self._log_info = log_info
        self._log_warning = log_warning
        self._log_debug = log_debug
        self._disable_progress_bar = disable_progress_bar
        self.info_messages = []
        self.warning_messages = []
        self.debug_messages = []
        self.custom_messages = defaultdict(set)
        self.progress_bar = None
        self.session = session
        self._spinner = None

    def start_spinner(self):
        if self._log_info:
            self._spinner = Spinner()
            self._spinner.start()

    def stop_spinner(self):
        if self._spinner:
            self._spinner.stop()

    def disable_warnings(self):
        self._log_warning = False

    def disable_info(self):
        self._log_info = False

    def enable_warnings(self):
        self._log_warning = True

    def enable_info(self):
        self._log_info = True

    def log_info(self, value: str):
        if self._log_info:
            self.logger.info(value)
        self.info_messages.append(value)

    def log_warning(self, value: str):
        if self._log_warning:
            self.logger.warning(value)
        self.warning_messages.append(value)

    def log_debug(self, value: str):
        if self._log_debug:
            self.logger.debug(value)
        self.debug_messages.append(value)

    def start_progress(
        self,
        iterations: Union[int, range],
        description: str = "Processing",
        disable=False,
    ):
        self.progress_bar = self.get_progress_bar(iterations, description, disable)

    @staticmethod
    def get_progress_bar(
        iterations: Union[int, range], description: str = "Processing", disable=False
    ):
        if isinstance(iterations, range):
            return tqdm.tqdm(iterations, desc=description, disable=disable)
        else:
            return tqdm.tqdm(total=iterations, desc=description, disable=disable)

    def finish_progress(self):
        self.progress_bar.close()

    def update_progress(self, value: int = 1):
        if self.progress_bar:
            self.progress_bar.update(value)

    def generate_report(self) -> str:
        report = ""
        if self.info_messages:
            report += "\n".join(self.info_messages)
        if self.warning_messages:
            report += "\n".join(self.warning_messages)
        return report

    def store_message(self, key: str, value: str):
        self.custom_messages[key].add(value)

    @property
    def messages(self):
        for key, values in self.custom_messages.items():
            yield f"{key} [{', '.join(values)}]"

    def track(self, key, value):
        if self.session:
            self.session[key] = value


class Progress:
    def __init__(self, iterations: Union[int, range], description: str = "Processing"):
        self._iterations = iterations
        self._description = description
        self._progress_bar = None

    def __enter__(self):
        return self

    def __exit__(self, type, value, traceback):
        if self._progress_bar:
            self._progress_bar.close()

    def update(self, value=1):
        if not self._progress_bar:
            self._progress_bar = Reporter.get_progress_bar(
                self._iterations, self._description
            )
        self._progress_bar.update(value)
