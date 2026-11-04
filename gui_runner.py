# -*- coding: utf-8 -*-
"""GUI 子进程执行器: 按 _gui_jobs.json 中的课程/章节选择执行刷课,
通过 stdout 的 [GUI] 前缀行向界面实时汇报进度"""
import json
import os
import sys
import traceback

os.chdir(os.path.dirname(os.path.abspath(__file__)))

import main
from api.answer import Tiku
from api.base import Account, Chaoxing


def emit(*parts):
    print("[GUI]", *parts, flush=True)


def run():
    with open(sys.argv[1], encoding="utf-8") as f:
        cfg = json.load(f)

    account = Account(cfg.get("username") or "", cfg.get("password") or "")
    tiku = Tiku.get_tiku_from_config(None)  # 自动读取 config.ini 中的题库(如有)
    tiku.init_tiku()
    chaoxing = Chaoxing(account=account, tiku=tiku, query_delay=0)

    result = chaoxing.login(login_with_cookies=bool(cfg.get("use_cookies")))
    if not result["status"]:
        print(f"登录失败: {result['msg']}", flush=True)
        sys.exit(1)

    # 包装 main.process_chapter, 汇报每个章节的开始/结束
    orig_process_chapter = main.process_chapter

    def wrapped(cx, course, point, speed):
        emit("chapter_begin", course["courseId"], point["id"])
        r = orig_process_chapter(cx, course, point, speed)
        emit("chapter_end", course["courseId"], point["id"], r.name)
        return r

    main.process_chapter = wrapped

    config = {
        "speed": min(2.0, max(1.0, float(cfg.get("speed", 1.0)))),
        "jobs": int(cfg.get("jobs", 4)),
        "notopen_action": cfg.get("notopen_action", "retry"),
        "retry_interval": 1.0,
    }

    for course in cfg["courses"]:
        emit("course_begin", course["courseId"])
        point_list = chaoxing.get_course_point(
            course["courseId"], course["clazzId"], course["cpi"])
        points = point_list["points"]
        wanted = course.get("chapters")  # None 表示整课全部章节
        if wanted is not None:
            id_set = set(wanted)
            points = [p for p in points if p["id"] in id_set]
        if not points:
            emit("course_skip", course["courseId"])
            continue
        tasks = [main.ChapterTask(index=i, point=p, course=course)
                 for i, p in enumerate(points)]
        main.JobProcessor(chaoxing, tasks, config).run()
    emit("done")


if __name__ == "__main__":
    try:
        run()
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        sys.exit(1)
