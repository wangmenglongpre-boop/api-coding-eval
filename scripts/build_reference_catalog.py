"""Build inert Python adaptations; never execute embedded solutions or checks."""

import ast
import json
from pathlib import Path
from textwrap import dedent


def source(text):
    return dedent(text).lstrip("\n")


CHECK_HEADER = source("""
    import ast
    import copy
    import functools
    import inspect
    import math
    import random
    import sys
    import unittest
    import warnings
    from datetime import datetime, timezone, timedelta
    import solution

    def _case(fn):
        @functools.wraps(fn)
        def run():
            try:
                fn()
            except Exception as exc:
                raise AssertionError(str(exc)) from exc
        return run
""")
MAINTAINABILITY = source("""
    @_case
    def check_maintainability():
        tree = ast.parse(inspect.getsource(solution))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, 'attr', '')
                assert name not in {'eval', 'exec'}
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                assert node.end_lineno - node.lineno + 1 <= 80
""")
TASKS = []


def add(number, title, project, language, pr, limit, requirements, public, reference, checks,
        negatives, rubric):
    TASKS.append({
        "id": f"C{number}", "scenario_id": f"C{number}",
        "title": title + "（Python 核心逻辑改编）",
        "source": f"参考 {project} PR #{pr}；原语言 {language}；Python 核心逻辑改编；"
                  f"https://github.com/{project}/pull/{pr}",
        "description": f"依据飞书《基础模型评测》参考题改编。{limit}"
                       "仅验证下述独立 Python 接口，不等同于原仓库隐藏测试或端到端评测。",
        "system_prompt": "按公开接口完成 solution.py，仅使用 Python 3.11 标准库。"
                         "只输出一个 python 围栏块，块内为完整 solution.py，不输出解释。",
        "requirements": requirements + "\n只修改 solution.py。禁止 eval/exec；任一函数至多80行"
                        "（含空行），作为可维护性代理指标。所有输入错误统一抛 ValueError，消息不限。",
        "public_files": {"solution.py": source(public)},
        "reference_files": {"solution.py": source(reference)},
        "test_files": {"checks.py": CHECK_HEADER + "\n" + source(checks) + "\n" + MAINTAINABILITY},
        "negative_files": {
            name: {"solution.py": source(reference).replace(old, new)}
            for name, (old, new) in negatives.items()
        },
        "rubric": [
            {"check": check, "dimension": dimension, "description": description,
             "critical": dimension == "security"}
            for (check, description), dimension in zip(rubric, [
                "functionality", "robustness", "security", "compliance", "maintainability",
            ])
        ],
        "output_format": {
            "kind": "files", "allowed_paths": ["solution.py"],
            "instructions": "只输出一个 ```python 围栏块，内容为完整 solution.py。",
        },
        "status": "draft",
    })


add(
    11, "Scrollbar：双轴尺寸、位移与自动更新", "element-plus/element-plus", "Vue3/TypeScript", 130,
    "保留 thumb 百分比尺寸、translate 双轴样式及 resize 状态考点；不运行 Vue DOM、包入口或浏览器测试。",
    """实现 geometry(viewport, content, offset=0, horizontal=False) -> dict；
viewport/content 为非负有限 int/float（不接受 bool），offset 为有限 int/float，horizontal 必须 bool。
overflow 时 size=100*viewport/content，move=100*clamp(offset,0,content-viewport)/viewport；
viewport=0 或 content<=viewport 时 size=move=0。返回且仅返回 size、move、style；
style 垂直为 {'height':f'{size:g}%', 'transform':f'translateY({move:g}%)',
'webkitTransform':同transform}，水平用 width/translateX。无 overflow 仍返回零样式。
实现 Scrollbar(viewport, content, horizontal=False)，offset 初始0，
scroll(offset) 更新 offset 并返回 geometry，update(viewport,content) 更新尺寸、
保留并钳制旧 offset 并返回 geometry；snapshot() 返回当前独立 dict。
对象公开属性 viewport/content/offset/horizontal；offset 始终钳制在[0,max(0,content-viewport)]，
viewport=0 时 offset=0。非法 scroll/update 输入不得改变原状态。
禁止跨实例共享状态或复用可变返回值。构造器/三个方法参数名保持，snapshot 无额外参数。""",
    """
    def geometry(viewport, content, offset=0, horizontal=False):
        raise NotImplementedError

    class Scrollbar:
        def __init__(self, viewport, content, horizontal=False):
            raise NotImplementedError
        def scroll(self, offset):
            raise NotImplementedError
        def update(self, viewport, content):
            raise NotImplementedError
        def snapshot(self):
            raise NotImplementedError
    """,
    """
    import math

    def _number(value, nonnegative=False):
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError('finite number required')
        if nonnegative and value < 0:
            raise ValueError('nonnegative required')

    def geometry(viewport, content, offset=0, horizontal=False):
        _number(viewport, True)
        _number(content, True)
        _number(offset)
        if type(horizontal) is not bool:
            raise ValueError('bool required')
        size = move = 0
        if viewport > 0 and content > viewport:
            size = 100 * viewport / content
            move = 100 * min(max(offset, 0), content - viewport) / viewport
        axis, dimension = ('X', 'width') if horizontal else ('Y', 'height')
        transform = f'translate{axis}({move:g}%)'
        return {'size': size, 'move': move, 'style': {
            dimension: f'{size:g}%', 'transform': transform, 'webkitTransform': transform}}

    class Scrollbar:
        def __init__(self, viewport, content, horizontal=False):
            geometry(viewport, content, 0, horizontal)
            self.viewport, self.content, self.horizontal = viewport, content, horizontal
            self.offset = 0

        def scroll(self, offset):
            geometry(self.viewport, self.content, offset, self.horizontal)
            self.offset = min(max(offset, 0), max(0, self.content - self.viewport)) if self.viewport else 0
            return self.snapshot()

        def update(self, viewport, content):
            geometry(viewport, content, self.offset, self.horizontal)
            offset = min(self.offset, max(0, content - viewport)) if viewport else 0
            self.viewport, self.content, self.offset = viewport, content, offset
            return self.snapshot()

        def snapshot(self):
            return geometry(self.viewport, self.content, self.offset, self.horizontal)
    """,
    """
    @_case
    def check_geometry():
        for horizontal, key, axis in [(False,'height','Y'), (True,'width','X')]:
            for offset, move in [(0,0),(100,50),(300,150),(999,150),(-1,0)]:
                result = solution.geometry(200,500,offset,horizontal)
                assert result == {'size':40, 'move':move, 'style':{
                    key:'40%', 'transform':f'translate{axis}({move}%)',
                    'webkitTransform':f'translate{axis}({move}%)'}}
        s = solution.Scrollbar(200,500)
        assert s.scroll(100)['move'] == 50
        assert s.update(100,500)['move'] == 100
        assert s.update(100,120)['move'] == 20 and s.offset == 20
        assert s.update(600,120)['size'] == 0 and s.offset == 0

    @_case
    def check_boundaries():
        for v,c in [(0,500),(0,0),(200,200),(200,100)]:
            assert solution.geometry(v,c,10)['size'] == 0
            assert solution.geometry(v,c,10)['move'] == 0
        assert math.isclose(solution.geometry(3,7,2)['move'],200/3)
        for args in [(-1,3),(3,-1),(True,3),(3,'9'),(float('nan'),3),(3,float('inf'))]:
            with unittest.TestCase().assertRaises(ValueError):
                solution.geometry(*args)
        for offset in [True,'1',float('nan'),float('inf')]:
            with unittest.TestCase().assertRaises(ValueError):
                solution.geometry(2,5,offset)
        with unittest.TestCase().assertRaises(ValueError):
            solution.geometry(2,5,horizontal=1)

    @_case
    def check_state_integrity():
        a,b = solution.Scrollbar(200,500), solution.Scrollbar(200,500)
        a.scroll(100)
        snapshot = a.snapshot()
        snapshot['style']['height'] = 'corrupt'
        snapshot['move'] = -99
        assert a.snapshot()['style']['height'] == '40%'
        assert a.snapshot()['move'] == 50 and b.offset == 0
        for op in [lambda:a.scroll('x'), lambda:a.update(300,-1)]:
            before = (a.viewport,a.content,a.offset,a.snapshot())
            with unittest.TestCase().assertRaises(ValueError):
                op()
            assert (a.viewport,a.content,a.offset,a.snapshot()) == before

    @_case
    def check_contract():
        p = inspect.signature(solution.geometry).parameters
        assert list(p) == ['viewport','content','offset','horizontal']
        assert p['offset'].default == 0 and p['horizontal'].default is False
        assert list(inspect.signature(solution.Scrollbar).parameters) == ['viewport','content','horizontal']
        for method,names in [('scroll',['self','offset']),('update',['self','viewport','content']),('snapshot',['self'])]:
            assert list(inspect.signature(getattr(solution.Scrollbar,method)).parameters) == names
        result = solution.Scrollbar(200,500,horizontal=True).scroll(offset=100)
        assert set(result) == {'size','move','style'} and 'width' in result['style']
    """,
    {
        "wrong_move_denominator": ("move = 100 * min(max(offset, 0), content - viewport) / viewport",
                                   "move = 100 * min(max(offset, 0), content - viewport) / content"),
        "no_resize_clamp": ("offset = min(self.offset, max(0, content - viewport)) if viewport else 0",
                            "offset = self.offset"),
    },
    [("check_geometry","双轴原题几何样例与 resize 钳制"),("check_boundaries","零尺寸、无溢出及非法输入"),
     ("check_state_integrity","返回值独立、实例隔离、失败不写状态"),
     ("check_contract","公开签名、样式键及 webkitTransform 兼容"),
     ("check_maintainability","无 eval/exec，函数至多80行")],
)

add(
    12, "Faker：跨平台亚秒时间与共享随机源", "joke2k/faker", "Python", 1946,
    "保留 Windows 整秒与其他平台亚秒精度、共享生成路径考点；不安装完整 Faker provider。",
    """实现 random_timestamp(start,end,rng,platform='linux')；
start/end 为有限非负 int/float（非bool），start<=end；rng 有 randint/uniform 方法。
platform 为非空字符串，仅 'win32' 被视为 Windows。Windows 从闭区间
[ceil(start),floor(end)] 调 rng.randint 一次，返回 int；没有可用整数则 ValueError且不调rng。
其他平台调 rng.uniform(start,end) 一次，返回 float，保留亚秒部分。
实现 unix_time(start,end,rng,platform='linux')，共享 random_timestamp 行为；
date_time_between(start,end,rng,platform='linux',tz=timezone.utc) 返回
datetime.fromtimestamp(timestamp,tz)，tz 是 datetime.tzinfo 实例且不接受 None；
iso8601(start,end,rng,platform='linux') 返回 UTC 时间字符串，不带时区后缀，
格式 YYYY-MM-DDTHH:MM:SS，Windows 长度19；其他平台追加 .ffffff 长度26，即使微秒为0也保留。
适用 timestamp 范围[0,4102444800]；超范围 ValueError。
失败（含非法 tz）必须在调用rng前验证。仅推进注入rng，不修改全局 random 状态。
全部四个接口保留参数名及默认值；支持 keyword 调用，不使用系统时钟。""",
    """
    from datetime import timezone
    def random_timestamp(start,end,rng,platform='linux'):
        raise NotImplementedError
    def unix_time(start,end,rng,platform='linux'):
        raise NotImplementedError
    def date_time_between(start,end,rng,platform='linux',tz=timezone.utc):
        raise NotImplementedError
    def iso8601(start,end,rng,platform='linux'):
        raise NotImplementedError
    """,
    """
    import math
    from datetime import datetime, timezone, tzinfo

    def random_timestamp(start,end,rng,platform='linux'):
        for value in (start,end):
            if type(value) not in (int,float) or not math.isfinite(value) or not 0 <= value <= 4102444800:
                raise ValueError('timestamp invalid')
        if start > end or not isinstance(platform,str) or not platform:
            raise ValueError('interval or platform invalid')
        if platform == 'win32':
            low, high = math.ceil(start), math.floor(end)
            if low > high:
                raise ValueError('no integer timestamp')
            return int(rng.randint(low,high))
        return float(rng.uniform(start,end))

    def unix_time(start,end,rng,platform='linux'):
        return random_timestamp(start,end,rng,platform)

    def date_time_between(start,end,rng,platform='linux',tz=timezone.utc):
        if not isinstance(tz,tzinfo):
            raise ValueError('tzinfo required')
        return datetime.fromtimestamp(random_timestamp(start,end,rng,platform),tz)

    def iso8601(start,end,rng,platform='linux'):
        value = date_time_between(start,end,rng,platform)
        return value.isoformat(timespec='seconds' if platform == 'win32' else 'microseconds')[:-6]
    """,
    """
    class RNG:
        def __init__(self): self.calls = []
        def randint(self,a,b):
            self.calls.append(('randint',a,b))
            return a
        def uniform(self,a,b):
            self.calls.append(('uniform',a,b))
            return a+(b-a)/4

    @_case
    def check_precision():
        for platform in ['linux','darwin','win32']:
            rng = RNG()
            value = solution.unix_time(100,101,rng,platform)
            assert type(value) is (int if platform=='win32' else float)
            assert value == (100 if platform=='win32' else 100.25)
            assert rng.calls == [('randint' if platform=='win32' else 'uniform',100,101)]
            rng = RNG()
            text = solution.iso8601(100,101,rng,platform)
            assert text == ('1970-01-01T00:01:40' if platform=='win32' else '1970-01-01T00:01:40.250000')
            assert len(rng.calls) == 1
        assert solution.iso8601(0,0,RNG()) == '1970-01-01T00:00:00.000000'
        tz = timezone(timedelta(hours=8))
        assert solution.date_time_between(100,101,RNG(),tz=tz) == datetime(1970,1,1,8,1,40,250000,tz)

    @_case
    def check_intervals():
        for start,end in [(2,1),(-1,1),(0,float('inf')),(float('nan'),1),(True,2),('1',2),(0,4102444801)]:
            rng = RNG()
            with unittest.TestCase().assertRaises(ValueError):
                solution.random_timestamp(start,end,rng)
            assert rng.calls == []
        rng = RNG()
        with unittest.TestCase().assertRaises(ValueError):
            solution.random_timestamp(1.1,1.9,rng,'win32')
        assert rng.calls == []
        assert solution.random_timestamp(1.1,2.1,rng,'win32') == 2
        assert rng.calls == [('randint',2,2)]
        for platform in ['',None,1]:
            rng = RNG()
            with unittest.TestCase().assertRaises(ValueError):
                solution.unix_time(0,1,rng,platform)
            assert not rng.calls

    @_case
    def check_rng_integrity():
        before = random.getstate()
        for fn in [solution.random_timestamp,solution.unix_time,solution.iso8601,solution.date_time_between]:
            a,b = random.Random(123),random.Random(123)
            assert fn(100,200,a) == fn(100,200,b)
        assert random.getstate() == before
        for tz in [None,'UTC',8]:
            rng = RNG()
            with unittest.TestCase().assertRaises(ValueError):
                solution.date_time_between(0,1,rng,tz=tz)
            assert not rng.calls

    @_case
    def check_contract():
        for name in ['random_timestamp','unix_time','iso8601','date_time_between']:
            p = inspect.signature(getattr(solution,name)).parameters
            assert list(p) == ['start','end','rng','platform'] + (['tz'] if name=='date_time_between' else [])
            assert p['platform'].default == 'linux'
        assert inspect.signature(solution.date_time_between).parameters['tz'].default == timezone.utc
        assert solution.unix_time(start=100,end=101,rng=RNG(),platform='win32') == 100
        assert len(solution.iso8601(100,101,RNG(),'win32')) == 19
        assert len(solution.iso8601(100,101,RNG(),'darwin')) == 26
    """,
    {
        "truncate_subseconds": ("return float(rng.uniform(start,end))", "return float(int(rng.uniform(start,end)))"),
        "windows_uniform": ("return int(rng.randint(low,high))", "return float(rng.uniform(low,high))"),
    },
    [("check_precision","平台精度、共享随机路径与时区转换"),("check_intervals","区间、类型及整数窗口"),
     ("check_rng_integrity","注入随机源复现、全局状态不变、失败不消耗随机源"),
     ("check_contract","签名与 Windows/non-Windows 字符串兼容"),
     ("check_maintainability","无 eval/exec，函数至多80行")],
)

add(
    13, "Prometheus：32位 k 与 int64 histogram", "prometheus/prometheus", "Go", 15548,
    "保留巨大 k 与 histogram int64 数据完整性；显式模拟32位目标，不运行 Go386、race、TSDB/OOO/原子对齐。",
    """实现 select_k(samples,k,mode='topk',word_bits=64) -> list；
samples 是 int/float 列表，非bool，允许 +/-inf；NaN 忽略。k 为非负 int（非bool），
不超过 2**63-1；word_bits 仅32或64（非bool），只代表目标字宽，k 不得被窄化或绕回。
topk 按值降序、bottomk 升序，平局保持原输入次序；k>=非NaN样本数返回所有，
k=0返回空，不分配长度为k的结构；非法 mode（仅topk/bottomk）抛ValueError。
实现 merge_histograms(histograms) -> {'count':int,'buckets':list[int]}；
每项字典且仅包含 count/buckets；非负整数均不超过2**63-1且非bool，
count 必须等于 buckets 之和，所有 histogram buckets 长度一致；
逐桶加和并求 count，任何最终 int64 溢出 ValueError，不窄化为int32。
空输入返回 {'count':0,'buckets':[]}；允许零桶且count=0。
所有操作不改变输入或复用输入 buckets，验证失败亦不得写入输入。保持签名/default。""",
    """
    def select_k(samples,k,mode='topk',word_bits=64):
        raise NotImplementedError
    def merge_histograms(histograms):
        raise NotImplementedError
    """,
    """
    import math
    MAX_INT64 = 2**63-1

    def _integer(value):
        if type(value) is not int or not 0 <= value <= MAX_INT64:
            raise ValueError('int64 required')

    def select_k(samples,k,mode='topk',word_bits=64):
        _integer(k)
        if type(word_bits) is not int or word_bits not in (32,64) or mode not in ('topk','bottomk'):
            raise ValueError('mode or word bits invalid')
        if not isinstance(samples,list):
            raise ValueError('list required')
        values = []
        for value in samples:
            if type(value) not in (int,float):
                raise ValueError('numeric sample required')
            if type(value) is int or not math.isnan(value):
                values.append(value)
        return sorted(values,reverse=mode=='topk')[:min(k,len(values))]

    def merge_histograms(histograms):
        if not isinstance(histograms,list):
            raise ValueError('list required')
        count, buckets = 0, None
        for hist in histograms:
            if not isinstance(hist,dict) or set(hist) != {'count','buckets'}:
                raise ValueError('histogram invalid')
            _integer(hist['count'])
            if not isinstance(hist['buckets'],list):
                raise ValueError('buckets invalid')
            for value in hist['buckets']:
                _integer(value)
            if sum(hist['buckets']) != hist['count']:
                raise ValueError('count mismatch')
            if buckets is None:
                buckets = [0]*len(hist['buckets'])
            if len(buckets) != len(hist['buckets']):
                raise ValueError('schema mismatch')
            count += hist['count']
            _integer(count)
            buckets = [a+b for a,b in zip(buckets,hist['buckets'])]
            for value in buckets:
                _integer(value)
        return {'count':count,'buckets':[] if buckets is None else buckets}
    """,
    """
    @_case
    def check_large_values():
        values = [3,1,2,float('nan'),float('inf'),float('-inf')]
        for bits in [32,64]:
            assert solution.select_k(values,9999999999,'bottomk',bits) == [float('-inf'),1,2,3,float('inf')]
            assert solution.select_k(values,2**63-1,'topk',bits) == [float('inf'),3,2,1,float('-inf')]
            assert solution.select_k(values,2,'topk',bits) == [float('inf'),3]
            assert solution.select_k(values,2,'bottomk',bits) == [float('-inf'),1]
        hist = [{'count':5000000000,'buckets':[3000000000,2000000000]},
                {'count':4000000000,'buckets':[2000000000,2000000000]}]
        assert solution.merge_histograms(hist) == {'count':9000000000,'buckets':[5000000000,4000000000]}
        assert solution.merge_histograms([{'count':2**63-1,'buckets':[2**63-1]}])['count'] == 2**63-1

    @_case
    def check_invalid():
        for k in [-1,True,1.0,2**63]:
            with unittest.TestCase().assertRaises(ValueError): solution.select_k([],k)
        for kwargs in [{'word_bits':16},{'word_bits':True},{'mode':'unknown'}]:
            with unittest.TestCase().assertRaises(ValueError): solution.select_k([],1,**kwargs)
        for samples in [[True],['1'],None]:
            with unittest.TestCase().assertRaises(ValueError): solution.select_k(samples,1)
        bad = [
            [{'count':1,'buckets':[0]}], [{'count':True,'buckets':[1]}],
            [{'count':1,'buckets':[True]}], [{'count':-1,'buckets':[-1]}],
            [{'count':0,'buckets':[]} , {'count':1,'buckets':[1]}],
            [{'count':2**63-1,'buckets':[2**63-1]}, {'count':1,'buckets':[1]}],
            [{'count':0,'buckets':[], 'extra':1}], [{'count':0,'buckets':()}], None,
        ]
        for hist in bad:
            with unittest.TestCase().assertRaises(ValueError): solution.merge_histograms(hist)
        assert solution.merge_histograms([]) == {'count':0,'buckets':[]}
        assert solution.select_k([float('nan')],10) == [] and solution.select_k([2],0) == []

    @_case
    def check_input_integrity():
        values = [4,1,4,2]
        assert solution.select_k(values,3) == [4,4,2] and values == [4,1,4,2]
        hist = [{'count':4,'buckets':[3,1]}, {'count':5,'buckets':[2,3]}]
        before = copy.deepcopy(hist)
        result = solution.merge_histograms(hist)
        result['buckets'][0] = 0
        assert hist == before
        bad = hist + [{'count':99,'buckets':[0,0]}]
        snapshot = copy.deepcopy(bad)
        with unittest.TestCase().assertRaises(ValueError): solution.merge_histograms(bad)
        assert bad == snapshot

    @_case
    def check_contract():
        p = inspect.signature(solution.select_k).parameters
        assert list(p) == ['samples','k','mode','word_bits']
        assert p['mode'].default == 'topk' and p['word_bits'].default == 64
        assert list(inspect.signature(solution.merge_histograms).parameters) == ['histograms']
        result = solution.select_k(samples=[1,1.0,2],k=2,mode='bottomk',word_bits=32)
        assert type(result[0]) is int and type(result[1]) is float
        assert solution.merge_histograms(histograms=[{'count':0,'buckets':[]}]) == {'count':0,'buckets':[]}
    """,
    {
        "int32_k_wrap": ("values = []", "k = (k + 2**31) % 2**32 - 2**31\n    values = []"),
        "int32_histogram_wrap": ("return {'count':count,'buckets':[] if buckets is None else buckets}",
                                "return {'count':count % 2**32,'buckets':[] if buckets is None else [v % 2**32 for v in buckets]}"),
    },
    [("check_large_values","9999999999 k、32位模拟与 int64 histogram"),
     ("check_invalid","非法值、NaN、空输入、schema与溢出"),
     ("check_input_integrity","不改输入、无返回值别名、失败不写入"),
     ("check_contract","排序平局稳定、类型与签名"),
     ("check_maintainability","无 eval/exec，函数至多80行")],
)

add(
    14, "ShardingSphere：无别名子查询分组修复", "apache/shardingsphere", "Java", 13809,
    "保留仅过滤 null alias 的精确修复语义；不运行 Maven、Oracle 或原仓库隐藏测试。",
    """实现 group_subqueries(subqueries) -> dict[str,list[dict]]。
subqueries 为list，每项为包含 alias 键的dict；alias 为None或str（允许空串）。
排除且仅排除 alias is None 的行；不得为它生成默认别名。空串仍作为合法分组key。
重复别名聚为列表，分组key与组内行顺序均按输入首次出现次序；
其余字段（包括任意嵌套 payload）不做解释，组内每个对象应是原行对象本身。
不改变输入列表、行字典或嵌套字段；返回组列表是新建的，不得共享输入容器或跨调用复用。
缺alias、非dict行、非None/str别名（含0、False）均ValueError，失败不修改输入。
空输入或全None返回{}。保持唯一参数名subqueries。""",
    """
    def group_subqueries(subqueries):
        raise NotImplementedError
    """,
    """
    def group_subqueries(subqueries):
        if not isinstance(subqueries,list):
            raise ValueError('list required')
        result = {}
        for row in subqueries:
            if not isinstance(row,dict) or 'alias' not in row:
                raise ValueError('row invalid')
            alias = row['alias']
            if alias is not None and not isinstance(alias,str):
                raise ValueError('alias invalid')
            if alias is None:
                continue
            result.setdefault(alias,[]).append(row)
        return result
    """,
    """
    @_case
    def check_grouping():
        rows = [{'alias':None,'query':'Oracle pagination nested select'},
                {'alias':'t','query':'a'},{'alias':'','query':'blank'},
                {'alias':'t','query':'b'},{'alias':None},{'alias':'x'}]
        result = solution.group_subqueries(rows)
        assert list(result) == ['t','','x']
        assert result == {'t':[rows[1],rows[3]], '':[rows[2]], 'x':[rows[5]]}
        assert solution.group_subqueries([{'alias':None}]) == {}

    @_case
    def check_invalid():
        for rows in [None,{},[None],[{}],[{'alias':0}],[{'alias':False}],[{'alias':[]}]]:
            with unittest.TestCase().assertRaises(ValueError): solution.group_subqueries(rows)
        assert solution.group_subqueries([]) == {}
        assert solution.group_subqueries([{'alias':''}]) == {'':[{'alias':''}]}

    @_case
    def check_input_integrity():
        rows = [{'alias':'t','payload':{'alias':None,'items':[1,2]}},{'alias':None}]
        before = copy.deepcopy(rows)
        result = solution.group_subqueries(rows)
        assert result['t'][0] is rows[0]
        result['t'].clear()
        assert rows == before
        assert solution.group_subqueries(rows)['t'] == [rows[0]]
        bad = rows + [{'alias':42}]
        snapshot = copy.deepcopy(bad)
        with unittest.TestCase().assertRaises(ValueError): solution.group_subqueries(bad)
        assert bad == snapshot

    @_case
    def check_contract():
        assert list(inspect.signature(solution.group_subqueries).parameters) == ['subqueries']
        rows = [{'alias':'B'},{'alias':'A'},{'alias':'B'},{'alias':''}]
        result = solution.group_subqueries(subqueries=rows)
        assert list(result) == ['B','A','']
        assert result['B'][0] is rows[0] and result['B'][1] is rows[2]
        assert type(result) is dict and all(type(group) is list for group in result.values())
    """,
    {
        "null_alias_not_filtered": ("if alias is None:\n            continue", "if alias is None:\n            raise ValueError('null key')"),
        "truthiness_alias_filter": ("if alias is None:\n            continue", "if not alias:\n            continue"),
    },
    [("check_grouping","Oracle 无别名输入、重复分组与空串保留"),
     ("check_invalid","null与空串精确区分、非法输入"),
     ("check_input_integrity","原行身份、输入不变、组容器独立"),
     ("check_contract","签名与首次出现次序"),
     ("check_maintainability","无 eval/exec，函数至多80行")],
)

add(
    15, "aiohttp：cleanup_closed 版本与弃用警告", "aio-libs/aiohttp", "Python", 9726,
    "保留 Python patch 版本边界、警告类别/位置与构造器状态；不运行 aiohttp 原172项集成测试。",
    """实现 needs_cleanup_closed(version) -> bool；
version 为3个非负整数组成的tuple，非bool；规则精确为：
(3,13,0)<=version<(3,13,1) or version<(3,12,7)。
模块常量 NEEDS_CLEANUP_CLOSED 按真实 sys.version_info[:3] 在导入时计算。
实现 Connector(enable_cleanup_closed=False,version=None)；
version=None 用真实sys.version_info[:3]，否则用注入tuple；
enable_cleanup_closed 必须bool。若 enable=True且不需要清理，发出且仅发出一条
DeprecationWarning，文本含 'cleanup_closed ignored'，stacklevel=2 指向调用构造器的位置，
并使 _cleanup_closed_disabled=True；其他情况无警告且 _cleanup_closed_disabled=not enable。
禁止修改全局warnings过滤器、sys.version_info或NEEDS_CLEANUP_CLOSED，跨实例状态独立。
非法输入抛ValueError且不发弃用警告。保持签名及默认参数。""",
    """
    NEEDS_CLEANUP_CLOSED = True
    def needs_cleanup_closed(version):
        raise NotImplementedError
    class Connector:
        def __init__(self,enable_cleanup_closed=False,version=None):
            raise NotImplementedError
    """,
    """
    import sys
    import warnings

    def needs_cleanup_closed(version):
        if not isinstance(version,tuple) or len(version) != 3:
            raise ValueError('version tuple required')
        if any(type(v) is not int or v < 0 for v in version):
            raise ValueError('version fields invalid')
        return (3,13,0) <= version < (3,13,1) or version < (3,12,7)

    NEEDS_CLEANUP_CLOSED = needs_cleanup_closed(sys.version_info[:3])

    class Connector:
        def __init__(self,enable_cleanup_closed=False,version=None):
            if type(enable_cleanup_closed) is not bool:
                raise ValueError('bool required')
            needs = needs_cleanup_closed(sys.version_info[:3] if version is None else version)
            self._cleanup_closed_disabled = not enable_cleanup_closed
            if enable_cleanup_closed and not needs:
                warnings.warn('cleanup_closed ignored',DeprecationWarning,stacklevel=2)
                self._cleanup_closed_disabled = True
    """,
    """
    VERSIONS = [((3,11,9),True),((3,12,0),True),((3,12,6),True),((3,12,7),False),
                ((3,12,99),False),((3,13,0),True),((3,13,1),False),((3,13,9),False),((3,14,0),False)]

    @_case
    def check_version_policy():
        for version,needs in VERSIONS:
            assert solution.needs_cleanup_closed(version) is needs
            for enabled in [False,True]:
                with warnings.catch_warnings(record=True) as caught:
                    warnings.simplefilter('always')
                    connector = solution.Connector(enabled,version)
                assert connector._cleanup_closed_disabled is (not enabled or not needs)
                assert len(caught) == int(enabled and not needs)
                if caught:
                    assert caught[0].category is DeprecationWarning
                    assert 'cleanup_closed ignored' in str(caught[0].message)

    @_case
    def check_invalid():
        for version in [None,[3,12,7],(3,12),(3,12,7,0),(-1,0,0),(True,12,7),(3,'12',7)]:
            with unittest.TestCase().assertRaises(ValueError):
                solution.needs_cleanup_closed(version)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            for enabled in [1,None,'true']:
                with unittest.TestCase().assertRaises(ValueError):
                    solution.Connector(enabled,(3,12,7))
            with unittest.TestCase().assertRaises(ValueError):
                solution.Connector(True,(3,12))
        assert not caught

    @_case
    def check_global_integrity():
        before = (sys.version_info,solution.NEEDS_CLEANUP_CLOSED,list(warnings.filters))
        a = solution.Connector(True,(3,11,0))
        b = solution.Connector(False,(3,11,0))
        b._cleanup_closed_disabled = False
        assert a._cleanup_closed_disabled is False
        assert solution.Connector(False,(3,11,0))._cleanup_closed_disabled is True
        assert (sys.version_info,solution.NEEDS_CLEANUP_CLOSED,list(warnings.filters)) == before

    @_case
    def check_warning_contract():
        p = inspect.signature(solution.Connector).parameters
        assert list(p) == ['enable_cleanup_closed','version']
        assert p['enable_cleanup_closed'].default is False and p['version'].default is None
        assert list(inspect.signature(solution.needs_cleanup_closed).parameters) == ['version']
        expected = (3,13,0) <= sys.version_info[:3] < (3,13,1) or sys.version_info[:3] < (3,12,7)
        assert solution.NEEDS_CLEANUP_CLOSED is expected
        assert solution.Connector()._cleanup_closed_disabled is True
        line = __EXPECTED_WARNING_LINE__
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            solution.Connector(enable_cleanup_closed=True,version=(3,12,7))
        assert len(caught) == 1
        assert caught[0].filename == __file__ and caught[0].lineno == line
    """,
    {
        "minor_version_only": ("return (3,13,0) <= version < (3,13,1) or version < (3,12,7)", "return version < (3,13,0)"),
        "wrong_warning_stacklevel": ("stacklevel=2", "stacklevel=1"),
    },
    [("check_version_policy","3.12.7/3.13.0/3.13.1 边界与状态"),
     ("check_invalid","版本类型与无效开关无警告"),
     ("check_global_integrity","实例隔离与全局依赖不变"),
     ("check_warning_contract","弃用警告调用位置、签名及真实版本常量"),
     ("check_maintainability","无 eval/exec，函数至多80行")],
)


if __name__ == "__main__":
    for task in TASKS:
        reference = task["reference_files"]["solution.py"]
        assert all(files["solution.py"] != reference for files in task["negative_files"].values())
        text = task["test_files"]["checks.py"]
        if "__EXPECTED_WARNING_LINE__" in text:
            tree = ast.parse(text)
            line = next(node.lineno for node in ast.walk(tree) if isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute) and node.func.attr == "Connector"
                        and any(k.arg == "enable_cleanup_closed" for k in node.keywords))
            task["test_files"]["checks.py"] = text.replace("__EXPECTED_WARNING_LINE__", str(line))
    destination = Path(__file__).resolve().parents[1] / "datasets/reference_tasks.json"
    destination.write_text(json.dumps(TASKS, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Generated {len(TASKS)} inert task definitions: {destination}")
