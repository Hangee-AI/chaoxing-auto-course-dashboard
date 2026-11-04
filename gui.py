# -*- coding: utf-8 -*-
"""超星学习通图形界面版

- 登录/课程列表/章节列表: 直接调用 api.base.Chaoxing
- 刷课执行: 子进程运行 gui_runner.py, 日志和章节进度实时回显
- 支持课程、章节两级勾选
"""
import ctypes
import json
import os
import queue
import re
import subprocess
import sys
import threading
import tkinter as tk
import tkinter.font
from tkinter import messagebox, ttk
from tkinter.scrolledtext import ScrolledText

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(SCRIPT_DIR)  # 保证 cookies.txt / config.ini / chaoxing.log 读写位置正确

CREATE_NO_WINDOW = 0x08000000
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
TQDM_RE = re.compile(r"^\s*\d+%\|")  # tqdm 进度条行
CHECKED, UNCHECKED = "☑ ", "☐ "
JOBS_FILE = os.path.join(SCRIPT_DIR, "_gui_jobs.json")


def hide_console_if_owned():
    """双击启动时隐藏黑色控制台窗口; 从终端启动时不处理"""
    if sys.platform != "win32":
        return
    try:
        k32 = ctypes.windll.kernel32
        buf = (ctypes.c_ulong * 4)()
        if k32.GetConsoleProcessList(buf, 4) <= 1:
            ctypes.windll.user32.ShowWindow(k32.GetConsoleWindow(), 0)
    except Exception:
        pass


class ChaoxingGUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("超星学习通")
        self.root.geometry("820x780")
        self.root.minsize(700, 640)

        self.queue: queue.Queue = queue.Queue()
        self.chaoxing = None          # 登录成功后的实例, 用于加载章节
        self.proc: subprocess.Popen | None = None
        self.logged_in = False
        self.loguru_hooked = False

        # 树节点状态
        self.item_kind: dict[str, str] = {}      # item -> course/chapter/placeholder
        self.item_course: dict[str, dict] = {}   # course item -> 课程信息
        self.item_point: dict[str, dict] = {}    # chapter item -> 章节信息
        self.item_base: dict[str, str] = {}      # item -> 不含勾选框的原始文本
        self.checked: dict[str, bool] = {}
        self.loaded_courses: set[str] = set()    # 已加载章节的课程 item
        self.loading_courses: set[str] = set()   # 正在加载的课程 item
        self.chapter_item: dict[tuple, str] = {} # (courseId, point_id) -> chapter item

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(100, self.poll_queue)

    # ---------------- UI ----------------
    def _build_ui(self):
        pad = {"padx": 8, "pady": 4}

        # 登录区
        login_frame = ttk.LabelFrame(self.root, text="登录")
        login_frame.pack(fill="x", **pad)
        ttk.Label(login_frame, text="手机号:").grid(row=0, column=0, sticky="e", padx=4, pady=6)
        self.username_var = tk.StringVar()
        ttk.Entry(login_frame, textvariable=self.username_var, width=18).grid(row=0, column=1, sticky="w")
        ttk.Label(login_frame, text="密码:").grid(row=0, column=2, sticky="e", padx=4)
        self.password_var = tk.StringVar()
        ttk.Entry(login_frame, textvariable=self.password_var, width=18, show="•").grid(row=0, column=3, sticky="w")
        self.use_cookies_var = tk.BooleanVar(value=os.path.exists(os.path.join(SCRIPT_DIR, "cookies.txt")))
        ttk.Checkbutton(login_frame, text="使用 cookies 登录", variable=self.use_cookies_var).grid(
            row=1, column=0, columnspan=2, sticky="w", padx=4, pady=(0, 6))
        self.login_btn = ttk.Button(login_frame, text="登录并获取课程", command=self.on_login)
        self.login_btn.grid(row=1, column=2, columnspan=2, sticky="e", padx=8, pady=(0, 6))

        # 课程/章节树
        tree_frame = ttk.LabelFrame(self.root, text="学习内容 (点击文字勾选, 点击 ▶ 展开章节)")
        tree_frame.pack(fill="both", expand=True, **pad)
        toolbar = ttk.Frame(tree_frame)
        toolbar.pack(fill="x", padx=4, pady=2)
        ttk.Button(toolbar, text="全选", command=lambda: self.set_all(True)).pack(side="left")
        ttk.Button(toolbar, text="全不选", command=lambda: self.set_all(False)).pack(side="left", padx=4)
        self.course_count_var = tk.StringVar(value="尚未获取课程")
        ttk.Label(toolbar, textvariable=self.course_count_var).pack(side="left", padx=12)

        tree_box = ttk.Frame(tree_frame)
        tree_box.pack(fill="both", expand=True, padx=4, pady=4)
        self.tree = ttk.Treeview(tree_box, show="tree", selectmode="browse")
        scroll = ttk.Scrollbar(tree_box, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.tree.tag_configure("done", foreground="#888888")
        self.tree.bind("<Button-1>", self.on_tree_click)

        # 参数区
        opt_frame = ttk.LabelFrame(self.root, text="参数")
        opt_frame.pack(fill="x", **pad)
        ttk.Label(opt_frame, text="视频倍速:").grid(row=0, column=0, sticky="e", padx=4, pady=6)
        self.speed_var = tk.StringVar(value="1.0")
        ttk.Combobox(opt_frame, textvariable=self.speed_var, width=6, state="readonly",
                     values=["1.0", "1.25", "1.5", "1.75", "2.0"]).grid(row=0, column=1, sticky="w")
        ttk.Label(opt_frame, text="并发章节数:").grid(row=0, column=2, sticky="e", padx=4)
        self.jobs_var = tk.StringVar(value="4")
        ttk.Spinbox(opt_frame, textvariable=self.jobs_var, from_=1, to=8, width=6).grid(row=0, column=3, sticky="w")
        ttk.Label(opt_frame, text="未开放章节:").grid(row=0, column=4, sticky="e", padx=4)
        self.notopen_var = tk.StringVar(value="重试")
        ttk.Combobox(opt_frame, textvariable=self.notopen_var, width=8, state="readonly",
                     values=["重试", "跳过"]).grid(row=0, column=5, sticky="w")

        # 操作区
        btn_frame = ttk.Frame(self.root)
        btn_frame.pack(fill="x", **pad)
        self.start_btn = ttk.Button(btn_frame, text="开始学习", command=self.on_start, state="disabled")
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(btn_frame, text="停止", command=self.on_stop, state="disabled")
        self.stop_btn.pack(side="left", padx=8)

        # 日志区
        log_frame = ttk.LabelFrame(self.root, text="运行日志")
        log_frame.pack(fill="both", expand=True, **pad)
        self.log_text = ScrolledText(log_frame, state="disabled", wrap="word", height=10,
                                     font=("Consolas", 9))
        self.log_text.pack(fill="both", expand=True, padx=4, pady=4)

        # 状态栏
        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(self.root, textvariable=self.status_var, relief="sunken",
                  anchor="w", padding=(6, 2)).pack(fill="x", side="bottom")

    # ---------------- 树操作 ----------------
    def set_item_text(self, item: str, suffix: str = ""):
        prefix = CHECKED if self.checked.get(item) else UNCHECKED
        self.tree.item(item, text=prefix + self.item_base.get(item, "") + suffix)

    def set_checked(self, item: str, value: bool):
        self.checked[item] = value
        self.set_item_text(item)

    def toggle_item(self, item: str):
        kind = self.item_kind.get(item)
        if kind not in ("course", "chapter"):
            return
        new_value = not self.checked.get(item, False)
        self.set_checked(item, new_value)
        if kind == "course":  # 课程勾选同步到已加载的章节
            for child in self.tree.get_children(item):
                if self.item_kind.get(ch) == "chapter":
                    self.set_checked(child, new_value)

    def on_tree_click(self, event):
        if self.proc:  # 刷课进行中不允许改动选择
            return
        item = self.tree.identify_row(event.y)
        if not item:
            return
        element = self.tree.identify_element(event.x, event.y)
        if "indicator" in element:  # 点的是展开箭头
            self.maybe_load_chapters(item)
            return
        self.toggle_item(item)

    def maybe_load_chapters(self, course_item: str):
        if (self.item_kind.get(course_item) != "course" or not self.chaoxing
                or course_item in self.loaded_courses or course_item in self.loading_courses):
            return
        self.loading_courses.add(course_item)
        for ch in self.tree.get_children(course_item):
            self.tree.delete(ch)
        ph = self.tree.insert(course_item, "end", text="正在加载章节...")
        self.item_kind[ph] = "placeholder"
        course = self.item_course[course_item]
        threading.Thread(target=self._load_chapters_worker,
                         args=(course_item, course), daemon=True).start()

    def _load_chapters_worker(self, course_item: str, course: dict):
        try:
            point_list = self.chaoxing.get_course_point(
                course["courseId"], course["clazzId"], course["cpi"])
            self.queue.put(("chapters", (course_item, course, point_list["points"], None)))
        except Exception as e:
            self.queue.put(("chapters", (course_item, course, [], str(e))))

    def fill_chapters(self, course_item: str, course: dict, points: list, error):
        self.loading_courses.discard(course_item)
        for ch in self.tree.get_children(course_item):  # 清掉占位项
            self.tree.delete(ch)
        if error:
            ph = self.tree.insert(course_item, "end", text=f"加载失败: {error}")
            self.item_kind[ph] = "placeholder"
            return
        self.loaded_courses.add(course_item)
        course_checked = self.checked.get(course_item, True)
        finished_count = 0
        for p in points:
            title = p["title"].strip()
            if p["has_finished"]:
                finished_count += 1
            base = title + ("（已完成）" if p["has_finished"] else "")
            item = self.tree.insert(course_item, "end", text="")
            self.item_kind[item] = "chapter"
            self.item_point[item] = p
            self.item_base[item] = base
            self.chapter_item[(course["courseId"], p["id"])] = item
            if p["has_finished"]:
                self.tree.item(item, tags=("done",))
            # 默认勾选: 课程已勾选 且 章节未完成
            self.checked[item] = course_checked and not p["has_finished"]
            self.set_item_text(item)
        total = len(points)
        self.append_log(f"《{course['title']}》共 {total} 个章节, 已完成 {finished_count} 个")

    def set_all(self, value: bool):
        for item in self.tree.get_children():
            if self.item_kind.get(item) == "course":
                self.set_checked(item, value)
                for child in self.tree.get_children(item):
                    if self.item_kind.get(child) == "chapter":
                        self.set_checked(child, value)

    # ---------------- 日志/队列 ----------------
    def append_log(self, text: str):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def poll_queue(self):
        while True:
            try:
                msg = self.queue.get_nowait()
            except queue.Empty:
                break
            # 消息长度不固定, 按 kind + 参数列表解包, 避免整批消息被丢弃
            kind, args = msg[0], msg[1:]
            try:
                if kind == "log":
                    self.append_log(args[0])
                elif kind == "status":
                    self.status_var.set(args[0])
                elif kind == "out":  # 子进程输出
                    self.handle_runner_output(ANSI_RE.sub("", args[0]).rstrip())
                elif kind == "session":
                    self.chaoxing = args[0]
                elif kind == "login_done":
                    self.on_login_done(*args)
                elif kind == "courses":
                    self.fill_tree(args[0])
                elif kind == "chapters":
                    self.fill_chapters(*args[0])
                elif kind == "proc_done":
                    self.on_proc_done(args[0])
            except Exception as e:
                self.append_log(f"[界面错误] {kind}: {e}")
        self.root.after(100, self.poll_queue)

    def handle_runner_output(self, line: str):
        if not line:
            return
        if line.startswith("[GUI]"):  # 进度汇报行
            parts = line.split()
            if len(parts) >= 4 and parts[1] == "chapter_begin":
                item = self.chapter_item.get((parts[2], parts[3]))
                if item:
                    self.set_item_text(item, "  ← 学习中...")
                    self.tree.see(item)
                self.status_var.set(f"学习中: {self.item_base.get(item, '')}")
            elif len(parts) >= 5 and parts[1] == "chapter_end":
                item = self.chapter_item.get((parts[2], parts[3]))
                result_text = {"SUCCESS": "  √ 完成", "ERROR": "  × 失败",
                               "NOT_OPEN": "  未开放"}.get(parts[4], f"  {parts[4]}")
                if item:
                    self.set_item_text(item, result_text)
            elif len(parts) >= 3 and parts[1] == "course_begin":
                self.append_log(f"----- 开始学习课程 ID: {parts[2]} -----")
            return
        if TQDM_RE.match(line):  # tqdm 进度条显示在状态栏
            self.status_var.set(line.strip())
        else:
            self.append_log(line)

    # ---------------- 登录 ----------------
    def on_login(self):
        username = self.username_var.get().strip()
        password = self.password_var.get().strip()
        use_cookies = self.use_cookies_var.get()
        if not use_cookies and (not username or not password):
            messagebox.showwarning("提示", "请输入手机号和密码 (或勾选使用 cookies 登录)")
            return
        self.login_btn.configure(state="disabled")
        self.status_var.set("正在登录...")
        threading.Thread(target=self._login_worker,
                         args=(username, password, use_cookies), daemon=True).start()

    def _login_worker(self, username, password, use_cookies):
        try:
            if not self.loguru_hooked:  # 核心库首次导入较慢, 且只需挂接一次日志
                self.queue.put(("log", "正在加载核心组件, 首次较慢请稍候..."))
                from api.logger import logger
                logger.add(lambda m: self.queue.put(("log", str(m).rstrip())),
                           level="INFO", format="{time:HH:mm:ss} | {level} | {message}")
                self.loguru_hooked = True
            from api.base import Account, Chaoxing

            chaoxing = Chaoxing(account=Account(username, password))
            result = chaoxing.login(login_with_cookies=use_cookies)
            if not result["status"]:
                self.queue.put(("login_done", False, result["msg"]))
                return
            self.queue.put(("session", chaoxing))
            self.queue.put(("log", "正在获取课程列表..."))
            courses = chaoxing.get_course_list()
            self.queue.put(("courses", courses))
            self.queue.put(("login_done", True, "登录成功"))
        except Exception as e:
            self.queue.put(("login_done", False, f"出现异常: {e}"))

    def on_login_done(self, ok: bool, msg: str):
        self.logged_in = ok
        self.login_btn.configure(state="normal")
        self.status_var.set(msg)
        self.append_log(("√ " if ok else "× ") + msg)
        self.refresh_start_btn()

    def refresh_start_btn(self):
        """登录成功 + 课程列表已加载 才允许开始"""
        ready = self.logged_in and bool(self.tree.get_children()) and not self.proc
        self.start_btn.configure(state="normal" if ready else "disabled")

    # ---------------- 课程/章节树填充 ----------------
    def fill_tree(self, courses: list[dict]):
        self.tree.delete(*self.tree.get_children())
        self.item_kind.clear()
        self.item_course.clear()
        self.item_point.clear()
        self.item_base.clear()
        self.checked.clear()
        self.loaded_courses.clear()
        self.loading_courses.clear()
        self.chapter_item.clear()

        id_count = {}
        for c in courses:
            id_count[c["courseId"]] = id_count.get(c["courseId"], 0) + 1

        for c in courses:
            base = f"{c['title']}  (ID: {c['courseId']})"
            if id_count[c["courseId"]] > 1:
                base += f"  [班级 {c['clazzId']}]"
            item = self.tree.insert("", "end", text="", open=False)
            self.item_kind[item] = "course"
            self.item_course[item] = c
            self.item_base[item] = base
            self.checked[item] = True
            self.set_item_text(item)
            ph = self.tree.insert(item, "end", text="(点击 ▶ 加载章节)")
            self.item_kind[ph] = "placeholder"
        self.course_count_var.set(f"共 {len(courses)} 门课程, 已全选")
        self.append_log(f"获取到 {len(courses)} 门课程, 展开课程可查看并勾选具体章节")
        self.refresh_start_btn()

    # ---------------- 刷课执行 ----------------
    def on_start(self):
        courses_payload = []
        for item in self.tree.get_children():
            if self.item_kind.get(item) != "course" or not self.checked.get(item):
                continue
            course = self.item_course[item]
            entry = {"courseId": course["courseId"], "clazzId": course["clazzId"],
                     "cpi": course["cpi"], "title": course["title"], "chapters": None}
            if item in self.loaded_courses:  # 展开过的课程按章节勾选
                ids = [self.item_point[ch]["id"] for ch in self.tree.get_children(item)
                       if self.item_kind.get(ch) == "chapter" and self.checked.get(ch)]
                if not ids:
                    continue
                entry["chapters"] = ids
            courses_payload.append(entry)

        if not courses_payload:
            messagebox.showwarning("提示", "请先勾选要学习的课程或章节")
            return

        payload = {
            "username": self.username_var.get().strip(),
            "password": self.password_var.get().strip(),
            "use_cookies": self.use_cookies_var.get(),
            "speed": float(self.speed_var.get()),
            "jobs": int(self.jobs_var.get()),
            "notopen_action": "continue" if self.notopen_var.get() == "跳过" else "retry",
            "courses": courses_payload,
        }
        with open(JOBS_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)

        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
        try:
            self.proc = subprocess.Popen(
                [sys.executable, "gui_runner.py", JOBS_FILE],
                cwd=SCRIPT_DIR, env=env,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                errors="replace", bufsize=1, creationflags=CREATE_NO_WINDOW,
            )
        except Exception as e:
            messagebox.showerror("启动失败", str(e))
            return

        chapter_count = sum(len(c["chapters"]) for c in courses_payload if c["chapters"])
        whole = sum(1 for c in courses_payload if not c["chapters"])
        desc = f"{chapter_count} 个章节" if chapter_count else ""
        desc += f"{'+' if chapter_count and whole else ''}{whole} 门整课" if whole else ""
        self.append_log(f"===== 开始学习: {desc} =====")
        self.status_var.set("学习进行中...")
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.login_btn.configure(state="disabled")
        threading.Thread(target=self._read_proc, args=(self.proc,), daemon=True).start()

    def _read_proc(self, proc: subprocess.Popen):
        for line in proc.stdout:  # 文本模式下 \r 也会分行, tqdm 进度条逐条读出
            self.queue.put(("out", line))
        self.queue.put(("proc_done", proc.wait()))

    def on_stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
            self.append_log("===== 已手动停止 =====")

    def on_proc_done(self, code: int):
        self.proc = None
        self.stop_btn.configure(state="disabled")
        self.login_btn.configure(state="normal")
        msg = "学习结束 (全部完成)" if code == 0 else f"学习结束 (退出码: {code})"
        self.status_var.set(msg)
        self.append_log(f"===== {msg} =====")
        self.refresh_start_btn()
        if code == 0:
            messagebox.showinfo("完成", "所有课程学习任务已完成")

    def on_close(self):
        if self.proc and self.proc.poll() is None:
            if not messagebox.askyesno("提示", "任务正在进行中, 确定要停止并退出吗?"):
                return
            self.proc.kill()
        self.root.destroy()


def main():
    hide_console_if_owned()
    try:
        ctypes.windll.user32.SetProcessDPIAware()  # 高分辨率下界面不模糊
    except Exception:
        pass
    root = tk.Tk()
    try:
        tk.font.nametofont("TkDefaultFont").configure(size=10)
    except Exception:
        pass
    ChaoxingGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
