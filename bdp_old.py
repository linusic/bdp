# 前提必须
# 配置 chrome.exe 环境变量
# 配置 chromedriver.exe 环境变量

# pip install selenium
# pip install rich
import argparse
import json
import os, sys, time, subprocess, signal, hashlib
import textwrap
from typing import Literal
from pathlib import Path
from functools import partial
from pprint import pprint

import asyncio

from concurrent.futures import ThreadPoolExecutor, wait, ALL_COMPLETED
from threading import Event
import multiprocessing

import urllib
from urllib.parse import quote
from urllib.request import urlopen

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By

import requests
import httpx
# from selenium.webdriver.chrome.service import Service


from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    TaskID,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)

from rich.console import Console
from rich.table import Table

from rich import print
from rich.filesize import decimal
from rich.text import Text
from rich.tree import Tree


BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(BASE_DIR)

from openapi_client.api import fileinfo_api, multimediafile_api
from openapi_client.api import auth_api
from openapi_client.api import fileupload_api
import openapi_client

driver_path = "D:/ide/chromedriver/chromedriver.exe"
driver_path_dir = Path(driver_path).parent

m3u8_store = "E:\download\pan_download\m3u8"


class BasePan:
    def __init__(self, app_id, app_key, secret_key, redirect_uri="oob", headless=True):
        self.app_id = app_id
        self.app_key = app_key
        self.secret_key = secret_key

        self.redirect_uri = redirect_uri

        self.code = None

        self.expires_in = None
        self.refresh_token = None
        self.access_token = None

        self.cookie = None
        self.headless = headless
        self.chrome_options = Options()

        """
            预创建 pan.baidu.com 的 Cookie
            需手动做一次(登录百度网盘+输入账户密码+短信验证码), 但永久, 信息保存在 --user-data-dir对应的文件夹里面)
            登陆后记得保存到 Chrome
            chrome.exe --remote-debugging-port=60006 --user-data-dir=D:\ide\chromedriver\save_cookie --headless --disable-gpu
            启用实验性 (持久化Cookie, 新账户)
        """
        open_port_cmd = f'chrome.exe ' \
                        f'--remote-debugging-port=60006 ' \
                        f'--user-data-dir={driver_path_dir / "save_cookie"}'

        if headless:
            # !!! headless 必须要在这里配置, 因为相当于把Driver托在了 --remote-debugging-port这个服务上, 所以应该配置的是它的 headless
            open_port_cmd += " --headless --disable-gpu"

        self.sub = subprocess.Popen(open_port_cmd)

        """
            因为用的是 remote-debugging-port, 所以普通的 headless无头配置 不起作用
            所以必须在上面的命令中 指定
            chrome_options.add_argument("--headless")
            chrome_options.add_argument('--disable-gpu')
        """
        self.chrome_options.add_experimental_option("debuggerAddress", "127.0.0.1:60006")
        """
            normal(默认,最慢): 会等待整个界面加载完成（包括JS触发事件,还包括对html和静态文件(JS,CSS,图片等), 但不包括ajax）
            eager: DOM树加载完成即可 (包括JS触发事件)
            none:  只解析 DOM 不包括触发事件 (速度快, 出错可能性极高, 通常配合 retrying 使用)
        """
        self.chrome_options.page_load_strategy = 'eager'  # 配置加载策略

        """
            若不用环境变量,则需要手动指定Driver位置,新版建议用Service类包装
            service = Service(executable_path=driver_path)
            driver = webdriver.Chrome(service=service, options=chrome_options)
        """
        self.driver = webdriver.Chrome(options=self.chrome_options)
        self.get_and_init_code()
        self.api_client = openapi_client.ApiClient(cookie=self.cookie)
        self.oauthtoken_authorizationcode()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.api_client.close()

    def get_and_init_code(self):
        get_code_url = f"http://openapi.baidu.com/oauth/2.0/authorize?response_type=code&client_id={self.app_key}&redirect_uri={self.redirect_uri}&scope=basic,netdisk&device_id={self.app_id}"

        self.driver.get(get_code_url)
        cookie_value = self.driver.get_cookie("BDUSS")["value"]
        self.cookie = f"BDUSS={cookie_value}"

        try:
            node = self.driver.find_element(By.CLASS_NAME, "input-text")
            self.code = node.get_property("value")
        except Exception as e:
            print("登录已过期,把headless置为False,在弹出WebUI上重写填入账户密码")
            exit()

        if self.headless:
            self.driver.close()  # headless模式下, 自动关闭 chrome.exe
        self.sub.kill()  # --remote-debugging-port=60006 必须 kill掉, 否则进程一直挂着, 下一次启动就会报错
        self.driver.quit()  # 关闭 chromedriver.exe (若不关, 任务管理器后台一大堆...)

    def oauthtoken_authorizationcode(self):
        api_instance = auth_api.AuthApi(self.api_client)

        try:
            res_dict = api_instance.oauth_token_code2token(self.code, self.app_key, self.secret_key,
                                                           self.redirect_uri)
            self.expires_in = res_dict["expires_in"]
            self.refresh_token = res_dict["refresh_token"]
            self.access_token = res_dict["access_token"]

        except openapi_client.ApiException as e:
            print("Exception when calling AuthApi->oauth_token_code2token: %s\n" % e)

    def oauthtoken_refreshtoken(self):
        api_instance = auth_api.AuthApi(self.api_client)
        try:
            api_response = api_instance.oauth_token_refresh_token(self.refresh_token, self.app_key, self.secret_key)
            # pprint(api_response)
        except openapi_client.ApiException as e:
            print("Exception when calling AuthApi->oauth_token_refresh_token: %s\n" % e)


class Pan(BasePan):
    def listall(self, path=None):
        api_instance = multimediafile_api.MultimediafileApi(self.api_client)
        path = path  # str |  "/"
        recursion = 1  # int |
        web = "1"  # str |  (optional)
        start = 0  # int |  (optional)
        limit = 1000  # int |  (optional)
        order = "time"  # str |  (optional)
        desc = 1  # int |  (optional)

        try:
            api_response = api_instance.xpanfilelistall(
                self.access_token, path, recursion, web=web, start=start, limit=limit, order=order, desc=desc)
            return api_response

        except openapi_client.ApiException as e:
            print("Exception when calling MultimediafileApi->xpanfilelistall: %s\n" % e)

    def filemetas(self, fsids):
        # ids_json_array is json_array  eg: [1,2,3]
        api_instance = multimediafile_api.MultimediafileApi(self.api_client)
        thumb = "1"  # str |  (optional)
        extra = "1"  # str |  (optional)
        dlink = "1"  # str |  (optional)
        needmedia = 1  # int |  (optional)

        try:
            api_response = api_instance.xpanmultimediafilemetas(
                self.access_token, fsids, thumb=thumb, extra=extra, dlink=dlink, needmedia=needmedia)
            return api_response
        except openapi_client.ApiException as e:
            print("Exception when calling MultimediafileApi->xpanmultimediafilemetas: %s\n" % e)

    def search(self, filename):
        api_instance = fileinfo_api.FileinfoApi(self.api_client)
        key = filename  # str |

        web = "1"  # str |  (optional)
        num = "2"  # str |  (optional)
        page = "1"  # str |  (optional)
        dir = "/"  # str |  (optional)
        recursion = "1"  # str |  (optional)

        try:
            api_response = api_instance.xpanfilesearch(
                self.access_token, key, web=web, num=num, page=page, dir=dir, recursion=recursion)
            return api_response

        except openapi_client.ApiException as e:
            print("Exception when calling FileinfoApi->xpanfilesearch: %s\n" % e)

    def filelist(self, file_name):

        api_instance = fileinfo_api.FileinfoApi(self.api_client)

        dir = "/"  # str |  (optional)
        folder = "0"  # str |  (optional)
        start = "0"  # str |  (optional)
        limit = 2  # int |  (optional)
        order = "time"  # str |  (optional)
        desc = 1  # int |  (optional)
        web = "web"  # str |  (optional)
        showempty = 1  # int |  (optional)

        # example passing only required values which don't have defaults set
        # and optional values
        try:
            api_response = api_instance.xpanfilelist(
                self.access_token, dir=dir, folder=folder, start=start, limit=limit, order=order, desc=desc,
                web=web,
                showempty=showempty)
            # pprint(api_response)
        except openapi_client.ApiException as e:
            print("Exception when calling FileinfoApi->xpanfilelist: %s\n" % e)


class Downloader():
    def __init__(self):
        self.progress = Progress(
            TextColumn("[bold blue]{task.fields[filename]}", justify="right"),
            BarColumn(bar_width=None),
            "[progress.percentage]{task.percentage:>3.1f}%",
            "•",
            DownloadColumn(),
            "•",
            TransferSpeedColumn(),
            "•",
            TimeRemainingColumn(),
            redirect_stderr=False,
            redirect_stdout=False
        )

        self.done_event = Event()

    def handle_sigint(self, signum, frame):
        self.done_event.set()
        subprocess.call(f"taskkill.exe /F /pid:{os.getpid()}", stderr=subprocess.PIPE, stdout=subprocess.PIPE)

    def copy_url(self, task_id: TaskID, url: str, path: str, headers=None) -> None:
        self.progress.console.log(f"Requesting {url}")
        if headers:
            req = urllib.request.Request(url=url, headers=headers)
        else:
            req = urllib.request.Request(url=url)
        response = urlopen(req)

        self.progress.update(task_id, total=int(response.info()["Content-length"]))
        with open(path, "wb") as dest_file:
            self.progress.start_task(task_id)
            for data in iter(partial(response.read, 32768), b""):
                dest_file.write(data)
                self.progress.update(task_id, advance=len(data))
                if self.done_event.is_set():
                    return
        self.progress.console.log(f"Downloaded {path}")

    def download(self, file_tuple_list, dest_dir: str, headers=None):
        with self.progress:
            pool = ThreadPoolExecutor(max_workers=4)
            for url, name, size, _,_ in file_tuple_list:
                if not name:
                    filename = url.split("/")[-1]
                else:
                    filename = name
                dest_path = os.path.join(dest_dir, filename)
                task_id = self.progress.add_task("download", filename=filename, start=False)

                pool.submit(self.copy_url, task_id, url, dest_path, headers)

            while not self.progress.finished:
                time.sleep(1)



class File:
    def scala_size(self, byte_size: int) -> Text:
        if len(str(round(byte_size))) > 12:
            scala_size = round(byte_size / 1024 ** 4, 2)
            unit = "TB"
            color = "yellow"
        elif len(str(round(byte_size))) > 9:
            scala_size = round(byte_size / 1024 ** 3, 2)
            unit = "GB"
            color = "red"
        elif len(str(round(byte_size))) > 6:
            scala_size = round(byte_size / 1024 ** 2, 2)
            unit = "MB"
            color = "blue"
        elif len(str(round(byte_size))) > 3:
            scala_size = round(byte_size / 1024, 2)
            unit = "KB"
            color = "green"
        else:
            scala_size = byte_size
            unit = "B"
            color = "green"

        size_str = f"{scala_size} {unit}"

        return Text(size_str, color, justify="right")

    def table_info(self, columns, rows_list, overflow: Literal["fold", "crop", "ellipsis", "ignore"] = "fold"):
        console = Console()
        table = Table(show_header=True, header_style="bold magenta", )
        for col in columns:  # "fold", "crop", "ellipsis"(默认)
            table.add_column(col, justify="left", overflow=overflow)  # 默认就是左对齐, 原API默认为ellipsis, 此处改为crop

        for id, filename, size, real_size, path_or_link  in rows_list:
            table.add_row(Text(id, "purple"), filename, self.scala_size(size), self.scala_size(real_size), path_or_link)

        console.print(table)

    @staticmethod
    def print_tree(folder_name, file_detail_list):
        # https://github.com/Textualize/rich/blob/master/examples/tree.py

        tree = Tree(
            Text(f"📂 {folder_name}", "purple"),
            # f":open_file_folder: [link file://{directory}]{directory}",
            guide_style="red",  # 树线条 颜色
        )

        for _, filename, size, real_size, _ in file_detail_list:
            text_filename = Text(filename, "green")
            # text_filename.highlight_regex(r"\..*$", "bold red")
            # text_filename.stylize(f"link file://{path}")
            # decimal 会自动处理文件单位
            text_filename.append(f" ({decimal(size)} => {decimal(real_size)})", "blue")
            icon = "📄"
            tree.add(Text(icon) + text_filename)

        print(tree)



class Upload(BasePan):
    def __init__(self, filename, remote_path, **kwargs):
        super().__init__(**kwargs)

        self.console = Console()

        self.done_event = Event()

        self.file_name = filename
        # 不需要, 因为服务端并不会反解析, 导致文件名乱了
        # self.remote_path = quote(f'/apps/{remote_path}')  # /apps/ 是硬性格式,必须这样 对应网盘的 "我的应用目录, 只能如此"
        self.remote_path = f'/apps/{remote_path}'  # /apps/ 是硬性格式,必须这样 对应网盘的 "我的应用目录, 只能如此"

        self.total_file_size = Path(self.file_name).resolve().stat().st_size  # int | size
        self.slice_size = 4 * 1024 * 1024

        self.md5_array = []

        self.temp_filename_list = []
        self.temp_file_f_list = []

        self.total_md5 = None  # count
        self.upload_id = None

        self.progress = Progress(
            TextColumn("[bold blue]{task.fields[filename]}", justify="right"),
            BarColumn(bar_width=None),
            "[progress.percentage]{task.percentage:>3.1f}%",
            "•",
            DownloadColumn(),
            "•",
            TransferSpeedColumn(),
            "•",
            TimeRemainingColumn(),
            redirect_stderr=False,
            redirect_stdout=False
        )

    def get_big_file_md5(self):
        m = hashlib.md5()
        with open(self.file_name, 'rb') as f:
            while True:
                data = f.read(4096)
                if not data:
                    break
                m.update(data)
        return m.hexdigest()

    # 4M 一组
    def generate_slice_md5_array(self):
        with open(self.file_name, 'rb') as f:
            m_total = hashlib.md5()  # 最后 和返回的MD5 用来 完整比较检验

            i = 0
            while True:
                m = hashlib.md5()  # 非全局, 每次循环覆盖, 不要更新
                data = f.read(self.slice_size)
                if not data:
                    break
                m.update(data)
                m_total.update(data)
                self.md5_array.append(m.hexdigest())

                temp_path = Path.cwd() / f"slice{i}"
                self.temp_filename_list.append(str(temp_path))
                temp_f = temp_path.open("wb+")  # 打开自动清空 (可读写, seek控制)
                temp_f.write(data)  # 暂不关闭,当缓存用
                temp_f.seek(0)  # 写完指针置0, 留给下面读
                self.temp_file_f_list.append(temp_f)  # 切片句柄保存起来
                i += 1

            self.total_md5 = m_total.hexdigest()

    def precreate(self):
        api_instance = fileupload_api.FileuploadApi(self.api_client)
        isdir = 0  # int | isdir
        autoinit = 1  # int | autoinit
        self.generate_slice_md5_array()
        block_list = json.dumps((self.md5_array))  # '[xx,xx]' json_array

        rtype = 1  # 关键字参数
        """
            0 表示不进行重命名，若云端存在同名文件返回错误
            1 表示当path冲突时，进行重命名
            2 表示当path冲突且block_list不同时，进行重命名
            3 当云端存在同名文件时，对该文件进行覆盖   
        """

        try:
            api_response = api_instance.xpanfileprecreate(
                self.access_token, self.remote_path, isdir, self.total_file_size, autoinit, block_list, rtype=rtype)
            self.upload_id = api_response["uploadid"]

            # pprint(api_response)
            # print(api_response)

        except openapi_client.ApiException as e:
            print("Exception when calling FileuploadApi->xpanfileprecreate: %s\n" % e)

    def upload(self, slice_index, f):
        api_instance = fileupload_api.FileuploadApi(self.api_client)
        type = "tmpfile"  # 固定

        api_response = None
        try:
            api_response = api_instance.pcssuperfile2(
                self.access_token, str(slice_index), self.remote_path, self.upload_id, type, file=f, async_req=True)
            # 怕大文件服务器相应慢, 不推荐用 ready()检测 , 改用下面的 successful()检测
            # while not api_response.ready():
            #     time.sleep(0.1)
        except openapi_client.ApiException as e:
            print("Exception when calling FileuploadApi->pcssuperfile2: %s\n" % e)

        flag = True
        while flag:
            time.sleep(1)
            try:
                api_response.successful()  # 若没准备好, successful 会主动抛 ValueError
                flag = False
                print("上传中")
            except ValueError as e:
                ...

        print(f"slice{slice_index}上传完成!")

    def create(self):
        with openapi_client.ApiClient() as api_client:
            api_instance = fileupload_api.FileuploadApi(api_client, )
            isdir = 0  # int | isdir    # 0 文件、1 目录
            block_list = json.dumps(self.md5_array)
            rtype = 1  # int  和 precreate 保持一直

            # print(block_list)
            try:
                while (api_response := api_instance.xpanfilecreate(
                        self.access_token, self.remote_path, isdir, self.total_file_size, self.upload_id, block_list,
                        rtype=rtype
                )["errno"]) == 10:
                    print(api_response)
                    time.sleep(1)

                print("返回结果是👇👇👇---------------------")
                pprint(api_response)
            except openapi_client.ApiException as e:
                print("Exception when calling FileuploadApi->xpanfilecreate: %s\n" % e)

    def upload_until_complete(self, slice_index, slice_content) -> None:
        remote_md5 = self.upload(slice_index, slice_content)
        print(self.md5_array[slice_index])
        print(remote_md5)

        if self.md5_array[slice_index] == remote_md5:

            self.console.log(f"Uploaded slice{slice_index}")
        else:
            self.console.log(f"Failed! bad slice{slice_index}")
            exit(-1)

    def handler_thread_task(self, slice_index, f):
        self.upload(slice_index, f)
        f.close()

    def rich_upload_per_slice(self):

        pool = ThreadPoolExecutor(max_workers=multiprocessing.cpu_count())

        futures = []
        for slice_index, f in enumerate(self.temp_file_f_list):
            future = pool.submit(self.handler_thread_task, slice_index, f)


            futures.append(future)



        wait(futures, return_when=ALL_COMPLETED)
        print("所有都上传完成了, 放行,并删除临时文件!")
        for file_path in self.temp_filename_list:
            Path(file_path).unlink(missing_ok=True)


def m3u8_download(m3u8_exe, m3u8_file_path, video_output_dir, new_video_name):

    # m3u8_file_path = r"E:\download\pan_download\m3u8\1.整体课程内容介绍.flv.m3u8"
    # video_output_dir  = r"E:\download\pan_download\video"
    # new_video_name  = "介绍2.mp4"

    options = "--enableDelAfterDone "  # 追加注意空格

    cmd = f'{m3u8_exe} "{m3u8_file_path}" --workDir "{video_output_dir}" --saveName "{new_video_name}" {options}'
    sp_obj = subprocess.Popen(cmd)
    return sp_obj


def callback_m3u8(future, m3u8_full_path=None):
    regular_path = str(m3u8_full_path).replace('\\', '/')

    response = future.result()
    try:
        result_m3u8: dict = response.json()
        # ad_token = result_m3u8["adToken"]
        if result_m3u8["error_code"] == 133:
            print(f"播放广告中... :{result_m3u8}")
        elif result_m3u8["error_code"] == 31341:
            print(f"转码中... :{result_m3u8}")
        elif result_m3u8["error_code"] == 31346:
            print(f"视频转码失败... :{result_m3u8}")
        elif result_m3u8["error_code"] == 31024:
            print(f"没有访问权限... :{result_m3u8}")
        elif result_m3u8["error_code"] == 31062:
            print(f"文件名无效，检查是否包含特殊字符... :{result_m3u8}")
        elif result_m3u8["error_code"] == 31066:
            print(f"文件不存在... :{result_m3u8}")
        elif result_m3u8["error_code"] == 31339:
            print(f"视频非法... :{result_m3u8}")

        console.log(Text(f"🛑{regular_path}", "red"))


    except json.JSONDecodeError:
        m3u8_text = response.text
        if m3u8_text.strip().endswith("#EXT-X-ENDLIST"):
            # 保存 M3U8文件
            m3u8_full_path.write_text(m3u8_text)
            console.log(f":open_file_folder: [link file://{regular_path}]{regular_path}")
        else:
            print(f"转码尚未完成, 稍后再试")
            console.log(Text(f"🛑{regular_path}", "red"))
    # console.log(str(m3u8_full_path), style="green")




async def request_m3u8(get_m3u8_url):
    async with httpx.AsyncClient() as client:
        response = await client.get(get_m3u8_url)
        return response


async def main_request_m3u8():
    tasks = []
    for path in file_path_list:
        file_name = path.split('/')[-1] + ".m3u8"
        m3u8_full_path = Path(m3u8_store) / file_name
        m3u8_full_path_list.append(str(m3u8_full_path))  # for download below

        urlencode_path = quote(path)

        get_code_url = f'https://pan.baidu.com/rest/2.0/xpan/file?method=streaming&access_token={pan.access_token}&path={urlencode_path}&type={m3u8_type}'

        result_with_ad: dict = requests.get(get_code_url, headers=headers).json()
        try:
            ad_token = result_with_ad["adToken"]
        except:
            print(f"{path} 不是视频, 无法捕获M3U8")
            sys.exit(-1)

        get_m3u8_url = f"{get_code_url}&adToken={quote(ad_token)}"  # adtoken 必须 urlencode必须 否则出不来

        task = asyncio.create_task(request_m3u8(get_m3u8_url))

        partial_callback_m3u8 = partial(callback_m3u8, m3u8_full_path=m3u8_full_path)
        task.add_done_callback(partial_callback_m3u8)
        tasks.append(task)

    tasks = asyncio.gather(*tasks)
    await tasks


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        prefix_chars='a',
        prog='LIN',
        # formatter_class=argparse.RawDescriptionHelpFormatter,
        formatter_class=argparse.RawTextHelpFormatter,
        usage="",
        description=textwrap.indent(r'''
        ┌───────────────Must Be Python3.6+───────────┐
        │ All Params Can Adjust In ->  ~/.config.cfg │
        │────────────────────────────────────────────│
        │ >> fa ad <DIR_NAME>                        │
        │ >> fa af <FILE_NAME>                       │
        └────────────────────────────────────────────┘''', " ")
    )
    # 防止 空格路径
    parser.add_argument('ad', dest="ad", nargs="+", type=str,
                        help="down all m3u8 files in directory")

    parser.add_argument('af', dest="af", nargs="+", type=str,
                        help="down one m3u8 file")

    parser.add_argument('at', dest="at", nargs="*", type=str,
                        help="request to transcode - async")

    parser.add_argument('as', dest="as", nargs="*", type=str,
                        help="show transcode status - async")

    args = parser.parse_args()

    file_mode = False
    dir_mode = False
    name = ""

    if args.af:
        file_mode = True
        name = ' '.join(args.af)  # 防止空格路径
    if args.ad:
        dir_mode = True
        name = ' '.join(args.ad)


    d = Downloader()
    signal.signal(signal.SIGINT, d.handle_sigint)

    f = File()

    with Pan(
            app_id="xxxxx",
            app_key="xxxx",
            secret_key="xxxx",
            redirect_uri="oob",
            headless=False  # default
    ) as pan:
        headers = {
            # 'User-Agent': 'pan.baidu.com',
            # Stream 需要 改UA 并添加 host
            # 'User-Agent': 'xpanvideo;netdisk;iPhone13;ios-iphone;15.1;ts',
            'User-Agent': 'xpanvideo;Mozilla/5.0 (iPhone; CPU iPhone OS 13_2_3 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/13.0.3 Mobile/15E148 Safari/604.1;ts',
            "Host": "pan.baidu.com",
            "Cookie": pan.cookie
        }

        # 不支持 VLC (但可下载)  480p  720p  (会员可 1080p)
        # m3u8_type = "M3U8_AUTO_480"  # 或  M3U8_AUTO_720
        # 支持 VLC在线播放 (也可下载)

        m3u8_exe  = r"E:\app\m3u8\N_m3u8DL-CLI_v3.0.2.exe"
        video_output_dir  = r"E:\download\pan_download\video"

        raw_quality = 1440
        use_quality = 480  # sd: 480;   hd:720  fhd: 1080  (均可用)
        # real_size = size * (use_quality / raw_quality)

        m3u8_type = f"M3U8_AUTO_{use_quality}"  # M3U8_AUTO_480 或 M3U8_AUTO_720

        ####################################  search + table

        search_json = pan.search(name)  # search_json["list"]
        search_file_or_dir_list = search_json["list"]

        if dir_mode:
            isdir = 1
        if file_mode:
            isdir = 0

        def parse_to_zip_file_detail(parse_get_list, isdir=0):
            fs_id_list = [_file["fs_id"] for _file in parse_get_list if _file["isdir"] == isdir]
            server_filename_list = [_file["server_filename"] for _file in parse_get_list if _file["isdir"] == isdir]
            size_list = [_file["size"] for _file in parse_get_list if _file["isdir"] == isdir]
            real_size_list = [_file["size"] * (use_quality / raw_quality) for _file in parse_get_list if _file["isdir"] == isdir]
            path_list = [_file["path"] for _file in parse_get_list if _file["isdir"] == isdir]

            return [*zip(fs_id_list, server_filename_list, size_list, real_size_list, path_list)]

        file_detail_list = parse_to_zip_file_detail(search_file_or_dir_list, isdir=isdir)

        ############################################ 获取详细信息 (非Stream, 普通下载, 主要获取 dlink)
        # 将 fs_id_list 拼成 API专属格式 json array  "[1,2,3]"
        # ids = json.dumps(fs_id_list)
        # file_metas_json = pan.filemetas(ids)
        # file_detail_list = file_metas_json["list"]

        # for file_detail in file_detail_list:
        #     pprint(file_detail)

        # files = [(str(index_1_based), file_detail["filename"], file_detail["dlink"],file_detail["size"]) for index_1_based, file_detail in
        #          enumerate(file_detail_list,start=1) if file_detail["isdir"] == 0]
        ############################################ ############################################



        # 0) fs_id_list
        # 1) server_filename_list
        # 2) path_list
        # 3) size_list
        files = [(str(index_1_based), file_detail[1], file_detail[2],file_detail[3], file_detail[4]) for index_1_based, file_detail in
                 enumerate(file_detail_list, start=1)]
        # f.table_info(["num", "folder_name", "folder_path", "size"], files, overflow="ignore")


        if dir_mode:
            while True:
                # 只推荐 ignore(截j断) 和 fold(向下铺) 这两种;    ellipsis 和 crop 对中文支持 有BUG
                f.table_info(["num", "folder_name", "size", f"{use_quality}p_size", "folder_path"], files, overflow="ignore")

                select_num = input("input the folder number>> ")
                try:
                    select_num = int(select_num) - 1

                    dir_path = files[select_num][4]  # index 4 is path

                    files_json = pan.listall(dir_path)
                    dir_file_list = files_json["list"]
                    # print(dir_file_list)

                    zip_file_list = parse_to_zip_file_detail(dir_file_list, isdir=0)

                    file_path_list = [file_detail["path"] for file_detail in dir_file_list]
                    f.print_tree(name, zip_file_list)

                    if input("make sure the filelist that will be downloaded (y/n)>> ").lower() == "y":
                        break
                    else:
                        continue

                except:
                    print(f"输入错误! 没有找到 {select_num}. 重新输入~")
        else:  # file_mode (per_file)
            while True:
                f.table_info(["num", "file_name", "size", f"{use_quality}p_size", "file_path"], files, overflow="ignore")
                select_num = input("input the file number>> ")
                try:
                    select_num = int(select_num) - 1

                    zip_file_list = [ files[select_num] ]  # 统一格式
                    file_path_list = [ files[select_num][4] ]   # 统一格式
                    f.print_tree("None(只文件模式)", zip_file_list)

                    if input("make sure the filelist that will be downloaded (y/n)>> ").lower() == "y":
                        break
                    else:
                        continue
                except:
                    print(f"输入错误! 没有找到 {select_num}. 重新输入~")

        m3u8_full_path_list = []
        console = Console()
        with console.status(f"[bold green]Downloading M3U8 ...", spinner="runner") as status:  # spinner="monkey"
            asyncio.run(main_request_m3u8())
            ######## asyncio.run()  自动 blocking .................

        if input("All M3U8 => MP4? (y/n)>> ").lower() == "y":

            executor = ThreadPoolExecutor(len(m3u8_full_path_list))

            future_list = []

            def call_back_for_download(future, new_video_name=None):
                future.result().communicate()

                # 如果有 "下载失败, 程序退出" 字样,  则 高亮出来

            for m3u8_file_path in m3u8_full_path_list:
                new_video_name = Path(m3u8_file_path).name
                # m3u8 => mp4
                print(new_video_name)

                future = executor.submit(m3u8_download,  m3u8_exe, m3u8_file_path, video_output_dir, new_video_name)
                future_list.append(future)

                callback_with_filename = partial(call_back_for_download, new_video_name=new_video_name)

                future.add_done_callback(callback_with_filename)
                # sp_obj = m3u8_download(m3u8_exe, m3u8_file_path, video_output_dir, new_video_name)


        # d.download(files, "./", headers=headers)


    # with Upload(
    #         filename="uploadtestdata/05.19-扩展库函数参数遍历字典PyDict_Keys并清理相应空.mp4",
    #         remote_path="123123/05.19-扩展库函数参数遍历字典PyDict_Keys并清理相应空.mp4",
    #         # filename="uploadtestdata/456.mp4", remote_path="123123/456.mp4",
    #         app_id="xxxxx",
    #         app_key="xxxx",
    #         secret_key="xxxx",
    #         redirect_uri="oob",
    #         headless=True  # default
    # ) as up:
    #     up.precreate()
    #     up.rich_upload_per_slice()
    #     up.create()
