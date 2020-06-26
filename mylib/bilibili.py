#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import os
import re
import shutil
import sys
from glob import glob
from http.cookiejar import MozillaCookieJar

import requests
# 下面导入的是通过pip安装的you-get
# 如果仅仅`import you_get`，模块名空间`sys.modules`中不会包含`you_get.util.strings`
# 但`you_get.util.strings`这个路径在后续用得到，you-get的另外几个模块需要从这个路径导入函数
import you_get.util.strings
from lxml import html

from .cli import SimpleDrawer
from .misc import safe_print, safe_basename
from .osutil import ensure_sigint_signal
from .tricks import modify_and_import
from .video import concat_videos, merge_m4s
from .web import cookie_str_from_dict, cookies_dict_from_file

BILIBILI_VIDEO_URL_PREFIX = 'https://www.bilibili.com/video/'


class BilibiliError(RuntimeError):
    pass


def _tmp(avid, cid):
    param = {'avid': avid, 'cid': cid, 'type': '', 'otype': 'json', 'fnver': 0, 'fnval': 16}
    api_url = 'https://api.bilibili.com/x/player/playurl'
    r = requests.get(api_url, param)
    return r.json()


# 这个函数用于修改you-get的B站下载模块`you_get.extractors.bilibili`的源码
def code_modify_you_get_bilibili(x: str):
    # `x`是输入的源代码字符串，就也是原版模块的源代码
    # 对其进行替换，在要替换的代码的上下文中，截取特征明显的一段，然后直接replace

    # 下面这段是添加4K视频的支持，若当前版本缺少4K视频流元数据， 其实就是在原来的字典开头加了一项
    x = x.replace('''
    stream_types = [
        {'id': 'flv_p60', 'quality': 116, 'audio_quality': 30280,
         'container': 'FLV', 'video_resolution': '1080p', 'desc': '高清 1080P60'},
''', '''
    stream_types = [
        {'id': 'hdflv2_4k', 'quality': 120, 'audio_quality': 30280,
         'container': 'FLV', 'video_resolution': '2160p', 'desc': '超清 4K'},
        {'id': 'flv_p60', 'quality': 116, 'audio_quality': 30280,
         'container': 'FLV', 'video_resolution': '1080p', 'desc': '高清 1080P60'},
''')
    # 下面也是跟4K相关的判断代码
    # B站视频流用不同数字ID标定不同的格式码率，120是4K，112则是大会员的1080P+（即较高码率的1080P30）
    # （160是大会员1080P60，这里的不需要对160作判断）
    x = x.replace('''
        elif height <= 1080 and qn <= 80:
            return 80
        else:
            return 112
''', '''
        elif height <= 1080 and qn <= 80:
            return 80
        elif height <= 1080 and qn <= 112:
            return 112
        else:
            return 120
''')
    # 下面这段修改了原本代码中的一行提示打印文本，以便和自己写的命令行工具所设计的选项保持一致
    x = x.replace('''
                log.w('This is a multipart video. (use --playlist to download all parts.)')
''', r'''
                sys.stderr.write('# multi-part video: use -p to download other part(s)\n')
''')
    # 下面这段修改了下载文件名的格式，原版是视频标题+选集子标题
    # 在视频标题+选集子标题的基础上，插入了一些有用的元信息：[av号][BV号][上传者用户名]
    x = x.replace('''
            # set video title
            self.title = initial_state['videoData']['title']
            # refine title for a specific part, if it is a multi-part video
            p = int(match1(self.url, r'[\?&]p=(\d+)') or match1(self.url, r'/index_(\d+)') or
                    '1')  # use URL to decide p-number, not initial_state['p']
            if pn > 1:
                part = initial_state['videoData']['pages'][p - 1]['part']
                self.title = '%s (P%s. %s)' % (self.title, p, part)
''', '''
            # set video title
            self.title = initial_state['videoData']['title']
            self.title += ' ' + self.get_vid_label() + self.get_author_label()
            # refine title for a specific part, if it is a multi-part video
            p = int(match1(self.url, r'[\?&]p=(\d+)') or match1(self.url, r'/index_(\d+)') or
                    '1')  # use URL to decide p-number, not initial_state['p']
            if pn > 1:
                part = initial_state['videoData']['pages'][p - 1]['part']
                self.title = '%s P%s. %s' % (self.title, p, part)
''')
    # 下面这段是个重点，修改的是原版中`you_get.extractors.bilibili.Bilibili.prepare_by_url`这个方法函数
    # 原版you-get对相当多的B站视频无法获取大会员的1080P+、1080P60等格式
    # 原版这里的逻辑有问题，按下面这样修改后，用到现在没发现异常
    # 根本原因没有彻底搞明白，但肯定与原版代码中`current_quality`和`best_quality`的判定有关
    x = x.replace('''
            # get alternative formats from API
            for qn in [112, 80, 64, 32, 16]:
                # automatic format for durl: qn=0
                # for dash, qn does not matter
                if current_quality is None or qn < current_quality:
''', '''
            # get alternative formats from API
            for qn in [116, 112, 80, 64, 32, 16]:
                # automatic format for durl: qn=0
                # for dash, qn does not matter
                # if current_quality is None or qn < current_quality:
                if True:
''')
    # 下面这段，修改的是原版`you_get.extractors.bilibili.Bilibili.prepare_by_url`的结尾部分
    # 新加一个流程，从已经获取的所有视频流格式中，删除一部分不需要的格式
    # you-get默认下载最佳画质，虽然可以选择画质，但用的格式名称比较长，不如数字ID本身来得方便
    # 所以加了一个`del_unwanted_dash_streams()`，用数字来指定最高画质和需要下载的画质
    # （其实加这个方法，还是因为Bug修得不彻底，权宜之计罢了）
    # 顺便一提，这个`del_unwanted_dash_streams`方法不是原版you-get代码里自带的
    # 而是在新的`YouGetBilibiliX`类里自定义的方法
    # 而`YouGetBilibiliX`则是继承的修改版`bilibili.Bilibili`
    # 替换原版代码 -> 调用一个原版没有的方法 -> 修改版 -> 继承修改版的新类 -> 在新类中补上这个缺失的方法
    # 回溯套娃，左右横跳！
    x = x.replace('''
    def prepare_by_cid(self,avid,cid,title,html_content,playinfo,playinfo_,url):
''', '''
        self.del_unwanted_dash_streams()

    def prepare_by_cid(self, avid, cid, title, html_content, playinfo, playinfo_, url):
''')
    return x


# 下面这个函数用于修改you-get的文件系统模块`you_get.util.fs`的源码
def code_modify_you_get_fs(x: str):
    # 原版为了兼容VFAT文件系统，会把文件名里面的方括号替换成圆括号
    # 修改版不需要这个特性，所以将这两行代码加#注释掉了
    x = x.replace("ord('['): '(',", "#ord('['): '(',")
    x = x.replace("ord(']'): ')',", "#ord(']'): ')',")
    # 下面把最大文件名字符串长度从80延长到200，原版的80有点小，200会不会太大尚不清楚
    x = x.replace('''
    text = text[:80] # Trim to 82 Unicode characters long
''', '''
    text = text[:200] # Trim to 82 Unicode characters long
''')
    return x


# 上面已经导入了原版的`you_get.util.strings`，这条模块路径很重要，另有几个模块依赖它
# 在此基础上，下面一行代码将原版的`you_get.util.fs`替换成修改版
you_get.util.fs = modify_and_import('you_get.util.fs', code_modify_you_get_fs)
# 接着把原版中的`you_get.util.strings.legitimize`函数替换成修改版`you_get.util.fs`中的对应函数
you_get.util.strings.legitimize = you_get.util.fs.legitimize
# （上面的`code_modify_you_get_fs`修改的就是`legitimize`的源码）
# 在原版的you-get中，`.util.string`从`.util.fs`中导入了`legitimize`这个函数
# `.util.string`又利用已经导入的`legitimize`和其他几个函数，构建了一个`get_filename`函数
# 而`you_get.extractor`和`you_get.common`又都用到了`get_filename`，当然是各自分别从`you_get.util.strings`导入的
# 因此，所以，故而，然则，
# 只要将原版`you_get.util.strings.legitimize`替换成修改版的`you_get.util.fs.legitimize`即可
# 那些从``you_get.util.strings`二次导入`legitimize`函数的模块会自动导入已被替换的修改版
# 综上所述，下面这行可以注释掉了
# you_get.extractor.get_filename = you_get.common.get_filename = you_get.util.strings.get_filename
# 下面则是将B站下载模块替换成修改版，所用的源码替换函数是`code_modify_you_get_bilibili`
you_get.extractors.bilibili = modify_and_import('you_get.extractors.bilibili', code_modify_you_get_bilibili)


# 搜寻av、BV、AV、bv开头的字符串或者整形数，将之变成B站视频的av嗯号或者BV号
def get_vid(x: str or int) -> str or None:
    if isinstance(x, int):
        vid = 'av{}'.format(x)
    elif isinstance(x, str):
        for p in (r'(av\d+)', r'(BV[\da-zA-Z]{10})'):
            m = re.search(p, x, flags=re.I)
            if m:
                vid = m.group(1)
                if vid.startswith('bv'):
                    vid = 'BV' + vid[2:]
                elif vid.startswith('AV'):
                    vid = 'av' + vid[2:]
                break
        else:
            vid = None
    else:
        raise TypeError("'{}' is not str or int".format(x))
    return vid


# `YouGetBilibiliX`继承了`you_get.extractors.bilibili.Bilibili`，添加了一些新的功能
# 其中包含了`del_unwanted_dash_streams`这个新方法
# 但是对`del_unwanted_dash_streams`的调用却是在被继承的`Bilibili`（的修改版）中进行的
class YouGetBilibiliX(you_get.extractors.bilibili.Bilibili):
    def __init__(self, *args, cookies: str or dict = None, qn_max=116, qn_single=None):
        super(YouGetBilibiliX, self).__init__(*args)
        self.cookie = None
        if cookies:
            self.set_cookie(cookies)
        self.qn_max = qn_max
        self.qn_single = qn_single
        self.html = None, None

    # B站视频的音频流分不同档次，选择中档128kbps就足够了，也可以选择最高音质
    # 低档30216码率偏低，30232约128kbps，30280可能是320kbps也可能是128kbps，貌似跟4K有关，尚不确定
    def set_audio_qn(self, qn):
        for d in self.stream_types:
            d['audio_quality'] = qn

    # 更新视频页面的HTML文档（超长字符串）
    def update_html_doc(self):
        url, doc = self.html
        if url != self.url:
            url = self.url
            headers = self.bilibili_headers()
            r = requests.get(url, headers=headers)
            doc = html.document_fromstring(r.text)
            self.html = url, doc

    # 设置cookies，大会员用得着
    # `cookie_str_from_dict`和`cookie_str_from_dict`这两个函数另有定义
    # 前者将cookies字典变成单字符串，后者负责读取cookies文件
    def set_cookie(self, cookies: str or dict):
        if isinstance(cookies, dict):
            c = cookie_str_from_dict(cookies)
        elif isinstance(cookies, str):
            if os.path.isfile(cookies):
                c = cookie_str_from_dict(cookies_dict_from_file(cookies))
            else:
                c = cookies
        else:
            raise TypeError("'{}' is not cookies file path str or joined cookie str or dict".format(cookies))
        self.cookie = c

    def bilibili_headers(self, referer=None, cookie=None):
        if not cookie:
            cookie = self.cookie
        headers = super(YouGetBilibiliX, self).bilibili_headers(referer=referer, cookie=cookie)
        return headers

    # 从URL和HTML获取av号BV号
    def get_vid(self):
        url = self.url
        for m in [re.search(r'/(av\d+)', url), re.search(r'/(bv\w{10})', url, flags=re.I)]:
            if m:
                vid = m.group(1)
                if vid.startswith('bv'):
                    vid = 'BV' + vid[2:]
                break
        else:
            vid = None
        return vid

    # [av号][BV号]
    def get_vid_label(self, fmt='[{}]'):
        the_vid = self.get_vid()
        label = fmt.format(the_vid)
        if the_vid.startswith('BV'):
            self.update_html_doc()
            _, h = self.html
            canonical = h.xpath('//link[@rel="canonical"]')[0].attrib['href']
            avid = re.search(r'/(av\d+)/', canonical).group(1)
            label += fmt.format(avid)
        return label

    # 上传者（UP主）用户名
    def get_author(self):
        self.update_html_doc()
        _, h = self.html
        return h.xpath('//meta[@name="author"]')[0].attrib['content']

    def get_author_label(self, fmt='[{}]'):
        return fmt.format(self.get_author())

    # 根据限定的最高画质或者选择的下载画质，从解析得到的视频流中，删除多余的、用不到的
    def del_unwanted_dash_streams(self):
        format_to_qn_id = {t['id']: t['quality'] for t in self.stream_types}
        for f in list(self.dash_streams):
            q = format_to_qn_id[f.split('-', maxsplit=1)[-1]]
            if q > self.qn_max or self.qn_single and self.qn_single == q:
                del self.dash_streams[f]


# 这是一个任务函数，包装了修改版的you-get的B站下载功能，供另外编写的命令行工具调用
def download_bilibili_video(url: str or int,
                            cookies: str or dict = None, output: str = None, parts: list = None,
                            qn_max: int = None, qn_single: int = None, moderate_audio: bool = True, fmt=None,
                            info: bool = False, playlist: bool = False, caption: bool = True,
                            **kwargs):
    # 确保在Windows操作系统中，SIGINT信号能够被传递到下层扩展中，从而确保Ctrl+C能够立即停止程序
    ensure_sigint_signal()
    dr = SimpleDrawer(sys.stderr.write, '\n')

    if not output:
        output = '.'
    if not qn_max:
        qn_max = 116
    url = BILIBILI_VIDEO_URL_PREFIX + get_vid(url)

    dr.hl()
    dr.print('{} -> {}'.format(url, output))
    dr.hl()
    bd = YouGetBilibiliX(cookies=cookies, qn_max=qn_max, qn_single=qn_single)

    if info:
        dl_kwargs = {'info_only': True}
    else:
        dl_kwargs = {'output_dir': output, 'merge': True, 'caption': caption}
    if fmt:
        dl_kwargs['format'] = fmt
    if moderate_audio:
        bd.set_audio_qn(30232)

    if playlist:
        bd.download_playlist_by_url(url, **dl_kwargs)
    else:
        if parts:
            base_url = url
            for p in parts:
                url = base_url + '?p={}'.format(p)
                dr.print(url)
                dr.hl()
                bd.download_by_url(url, **dl_kwargs)
        else:
            bd.download_by_url(url, **dl_kwargs)


def jijidown_rename_alpha(path: str, part_num=True):
    rename = os.rename
    isfile = os.path.isfile
    isdir = os.path.isdir
    basename = os.path.basename
    dirname = os.path.dirname
    path_join = os.path.join

    def _ren_file(filepath):
        name = basename(filepath)
        parent = dirname(filepath)
        print('{}:'.format(parent))
        new_name = re.sub(r'\.[Ff]lv\.mp4$', '.mp4', name)
        new_name = re.sub(r'^(\d+\.)?(.*?)\(Av(\d+).*?\)', r'\1 \2 [av\3]', new_name)
        if not part_num:
            new_name = re.sub(r'^\d+\.', '', new_name)
        # if new_name[-5:] == '].ass' and new_name[-8:-5] != '+弹幕':
        #     new_name = new_name[:-5] + '+弹幕].ass'
        # elif new_name[-5:] == '].xml' and new_name[-8:-5] != '+弹幕':
        #     new_name = new_name[:-5] + '+弹幕].xml'
        if new_name[-4:] == '.ass':
            new_name = new_name[:-4] + '.bilibili-danmaku-ass'
        elif new_name[-6:] == 'lv.mp4':
            new_name = new_name[:-8] + '.mp4'
        new_name = new_name.strip()
        print('{} -> {}'.format(name, new_name))
        new_filepath = path_join(parent, new_name)
        rename(filepath, new_filepath)

    if isfile(path):
        _ren_file(path)
    elif isdir(path):
        for i in [path_join(path, f) for f in os.listdir(path)]:
            _ren_file(i)
    else:
        print('Not exist: {}'.format(path))


class BilibiliAppCacheEntry:
    def __init__(self, vid_dir_path, cookies_file_path: str = None):
        if cookies_file_path:
            self.cookies = requests.utils.dict_from_cookiejar(MozillaCookieJar(cookies_file_path))
        else:
            self.cookies = None
        self.folder = vid_dir_path
        self.work_dir, self.id = os.path.split(os.path.realpath(vid_dir_path))
        self.part_list = os.listdir(vid_dir_path)
        self.part_sum = len(self.part_list)
        self._current_part = None
        self._current_meta = None

    def get_uploader(self):
        url = 'https://www.bilibili.com/video/av{}/'.format(self.id)
        param = {}
        if self.cookies:
            param['cookies'] = self.cookies
        r = requests.get(url, **param)
        h = html.document_fromstring(r.text)
        return h.xpath('//meta[@name="author"]')[0].attrib['content']

    # def get_uploader(self):
    #     url = 'https://www.bilibili.com/video/av{}/'.format(self.id)
    #     self.browser.visit(url)
    #     meta_author = self.browser.find_by_xpath('//meta[@name="author"]').first.outer_html
    #     author = meta_author.split('content="')[-1].split('"')[0]
    #     if author:
    #         return author
    #     else:
    #         # raise BilibiliError('No author found.')
    #         return ''

    def extract_part(self):
        print('+ {}'.format(self.folder))
        for part in self.part_list:
            self._current_part = part
            print('  + {}'.format(part), end=': ')
            try:
                self._current_meta = meta = json.load(
                    open(os.path.join(self.folder, part, 'entry.json'), encoding='utf8'))
            except FileNotFoundError:
                # os.remove(os.path.join(self.folder, part))
                print('    NO JSON META FOUND')
                continue
            if 'page_data' in meta:
                self.extract_vupload()
            elif 'ep' in meta:
                self.extract_bangumi()

    def extract_vupload(self):
        title = safe_basename(self._current_meta['title'])
        file_list = glob(os.path.join(self.folder, self._current_part, self._current_meta['type_tag'], '*'))
        ext_list = [f[-4:] for f in file_list]
        # try:
        #     uploader = '[{}]'.format(self.get_uploader())
        # except BilibiliError:
        #     uploader = ''
        uploader = '[{}]'.format(self.get_uploader() or 'NA')
        output = os.path.join(self.work_dir, '{} [av{}]{}'.format(title, self.id, uploader))
        if len(self.part_list) >= 2:
            part_title = safe_basename(self._current_meta['page_data']['part'])
            output += '{}-{}.mp4'.format(self._current_part, part_title)
        else:
            output += '.mp4'
        safe_print(output)
        if '.m4s' in ext_list:
            m4s_list = [f for f in file_list if f[-4:] == '.m4s']
            merge_m4s(m4s_list, output)
        elif '.blv' in ext_list:
            blv_list = [f for f in file_list if f[-4:] == '.blv']
            concat_videos(blv_list, output)
        else:
            print('    NO MEDIA STREAM FOUND')
        shutil.copy2(os.path.join(self.folder, self._current_part, 'danmaku.xml'), output[:-3] + 'xml')

    def extract_bangumi(self):
        title = safe_basename(self._current_meta['title'])
        blv_list = glob(os.path.join(self.folder, self._current_part, self._current_meta['type_tag'], '*.blv'))
        part_title = safe_basename(self._current_meta['ep']['index_title'])
        av_id = self._current_meta['ep']['av_id']
        ep_num = self._current_meta['ep']['index']
        output_dir = os.path.join(self.work_dir, '{} [av{}][{}]'.format(title, av_id, self.id))
        if not os.path.isdir(output_dir):
            os.mkdir(output_dir)
        output = os.path.join(output_dir, '{}. {}.mp4'.format(str(ep_num).zfill(len(str(self.part_sum))), part_title))
        safe_print(output)
        concat_videos(blv_list, output)
        shutil.copy2(os.path.join(self.folder, self._current_part, 'danmaku.xml'), output[:-3] + 'xml')
