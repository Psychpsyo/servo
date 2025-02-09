# Current code status:
#
# This was originally written by Philip Taylor for use at
# http://philip.html5.org/tests/canvas/suite/tests/
#
# It has been adapted for use with the Web Platform Test Suite suite at
# https://github.com/web-platform-tests/wpt/
#
# The original version had a number of now-removed features (multiple versions
# of each test case of varying verbosity, Mozilla mochitests, semi-automated
# test harness). It also had a different directory structure.

# To update or add test cases:
#
# * Modify the tests*.yaml files.
#  - 'name' is an arbitrary hierarchical name to help categorise tests.
#  - 'desc' is a rough description of what behaviour the test aims to test.
#  - 'code' is JavaScript code to execute, with some special commands starting
#    with '@'.
#  - 'expected' is what the final canvas output should be: a string 'green' or
#    'clear' (100x50 images in both cases), or a string 'size 100 50' (or any
#    other size) followed by Python code using Pycairo to generate the image.
#
# * Run "./build.sh".
# This requires a few Python modules which might not be ubiquitous.
# It will usually emit some warnings, which ideally should be fixed but can
# generally be safely ignored.
#
# * Test the tests, add new ones to Git, remove deleted ones from Git, etc.

from typing import Any, List, Mapping, MutableMapping, Optional, Set, Tuple

import re
import collections
import dataclasses
import enum
import importlib
import itertools
import jinja2
import os
import pathlib
import sys
import textwrap

try:
    import cairocffi as cairo  # type: ignore
except ImportError:
    import cairo

try:
    # Compatible and lots faster.
    import syck as yaml  # type: ignore
except ImportError:
    import yaml


class Error(Exception):
    """Base class for all exceptions raised by this module"""


class InvalidTestDefinitionError(Error):
    """Raised on invalid test definition."""


def _doubleQuoteEscape(string: str) -> str:
    return string.replace('\\', '\\\\').replace('"', '\\"')


def _escapeJS(string: str) -> str:
    string = _doubleQuoteEscape(string)
    # Kind of an ugly hack, for nicer failure-message output.
    string = re.sub(r'\[(\w+)\]', r'[\\""+(\1)+"\\"]', string)
    return string


def _unroll(text: str) -> str:
    """Unrolls text with all possible permutations of the parameter lists.

    Example:
    >>> print _unroll('f = {<a | b>: <1 | 2 | 3>};')
    // a
    f = {a: 1};
    f = {a: 2};
    f = {a: 3};
    // b
    f = {b: 1};
    f = {b: 2};
    f = {b: 3};
    """
    patterns = []  # type: List[Tuple[str, List[str]]]
    while True:
        match = re.search(r'<([^>]+)>', text)
        if not match:
            break
        key = f'@unroll_pattern_{len(patterns)}'
        values = text[match.start(1):match.end(1)]
        text = text[:match.start(0)] + key + text[match.end(0):]
        patterns.append((key, [value.strip() for value in values.split('|')]))

    def unroll_patterns(text: str,
                        patterns: List[Tuple[str, List[str]]],
                        label: Optional[str] = None) -> List[str]:
        if not patterns:
            return [text]
        patterns = patterns.copy()
        key, values = patterns.pop(0)
        return (['// ' + label] if label else []) + list(
            itertools.chain.from_iterable(
                unroll_patterns(text.replace(key, value), patterns, value)
                for value in values))

    result = '\n'.join(unroll_patterns(text, patterns))
    return result


def _expand_nonfinite(method: str, argstr: str, tail: str) -> str:
    """
    >>> print _expand_nonfinite('f', '<0 a>, <0 b>', ';')
    f(a, 0);
    f(0, b);
    f(a, b);
    >>> print _expand_nonfinite('f', '<0 a>, <0 b c>, <0 d>', ';')
    f(a, 0, 0);
    f(0, b, 0);
    f(0, c, 0);
    f(0, 0, d);
    f(a, b, 0);
    f(a, b, d);
    f(a, 0, d);
    f(0, b, d);
    """
    # argstr is "<valid-1 invalid1-1 invalid2-1 ...>, ..." (where usually
    # 'invalid' is Infinity/-Infinity/NaN).
    args = []
    for arg in argstr.split(', '):
        match = re.match('<(.*)>', arg)
        if match is None:
            raise InvalidTestDefinitionError(
                f"Expected arg to match format '<(.*)>', but was: {arg}")
        a = match.group(1)
        args.append(a.split(' '))
    calls = []
    # Start with the valid argument list.
    call = [args[j][0] for j in range(len(args))]
    # For each argument alone, try setting it to all its invalid values:
    for i in range(len(args)):
        for a in args[i][1:]:
            c2 = call[:]
            c2[i] = a
            calls.append(c2)
    # For all combinations of >= 2 arguments, try setting them to their
    # first invalid values. (Don't do all invalid values, because the
    # number of combinations explodes.)
    def f(c: List[str], start: int, depth: int) -> None:
        for i in range(start, len(args)):
            if len(args[i]) > 1:
                a = args[i][1]
                c2 = c[:]
                c2[i] = a
                if depth > 0:
                    calls.append(c2)
                f(c2, i + 1, depth + 1)

    f(call, 0, 0)

    return '\n'.join('%s(%s)%s' % (method, ', '.join(c), tail) for c in calls)


def _get_test_sub_dir(name: str, name_to_sub_dir: Mapping[str, str]) -> str:
    for prefix in sorted(name_to_sub_dir.keys(), key=len, reverse=True):
        if name.startswith(prefix):
            return name_to_sub_dir[prefix]
    raise InvalidTestDefinitionError(
        'Test "%s" has no defined target directory mapping' % name)


def _remove_extra_newlines(text: str) -> str:
    """Remove newlines if a backslash is found at end of line."""
    # Lines ending with '\' gets their newline character removed.
    text = re.sub(r'\\\n', '', text, flags=re.MULTILINE | re.DOTALL)

    # Lines ending with '\-' gets their newline and any leading white spaces on
    # the following line removed.
    text = re.sub(r'\\-\n\s*', '', text, flags=re.MULTILINE | re.DOTALL)
    return text

def _expand_test_code(code: str) -> str:
    code = _remove_extra_newlines(code)

    # Unroll expressions with a cross-product-style parameter expansion.
    code = re.sub(r'@unroll ([^;]*;)', lambda m: _unroll(m.group(1)), code)

    code = re.sub(r'@nonfinite ([^(]+)\(([^)]+)\)(.*)', lambda m:
                  _expand_nonfinite(m.group(1), m.group(2), m.group(3)),
                  code)  # Must come before '@assert throws'.

    code = re.sub(r'@assert pixel (\d+,\d+) == (\d+,\d+,\d+,\d+);',
                  r'_assertPixel(canvas, \1, \2);', code)

    code = re.sub(r'@assert pixel (\d+,\d+) ==~ (\d+,\d+,\d+,\d+);',
                  r'_assertPixelApprox(canvas, \1, \2, 2);', code)

    code = re.sub(r'@assert pixel (\d+,\d+) ==~ (\d+,\d+,\d+,\d+) \+/- (\d+);',
                  r'_assertPixelApprox(canvas, \1, \2, \3);', code)

    code = re.sub(r'@assert throws (\S+_ERR) (.*);',
                  r'assert_throws_dom("\1", function() { \2; });', code)

    code = re.sub(r'@assert throws (\S+Error) (.*);',
                  r'assert_throws_js(\1, function() { \2; });', code)

    code = re.sub(
        r'@assert (.*) === (.*);', lambda m: '_assertSame(%s, %s, "%s", "%s");'
        % (m.group(1), m.group(2), _escapeJS(m.group(1)), _escapeJS(m.group(2))
           ), code)

    code = re.sub(
        r'@assert (.*) !== (.*);', lambda m:
        '_assertDifferent(%s, %s, "%s", "%s");' % (m.group(1), m.group(
            2), _escapeJS(m.group(1)), _escapeJS(m.group(2))), code)

    code = re.sub(
        r'@assert (.*) =~ (.*);', lambda m: 'assert_regexp_match(%s, %s);' % (
            m.group(1), m.group(2)), code)

    code = re.sub(
        r'@assert (.*);', lambda m: '_assert(%s, "%s");' % (m.group(
            1), _escapeJS(m.group(1))), code)

    code = re.sub(r' @moz-todo', '', code)

    code = re.sub(r'@moz-UniversalBrowserRead;', '', code)

    assert ('@' not in code)

    return code


class TestType(str, enum.Enum):
    HTML_CANVAS = 'htmlcanvas'
    OFFSCREEN_CANVAS = 'offscreencanvas'
    WORKER = 'worker'


def _get_enabled_test_types(test: Mapping[str, Any]) -> Set[TestType]:
    return {TestType(t.lower()) for t in test.get('canvasType', TestType)}


@dataclasses.dataclass
class TestConfig:
    out_dir: str
    image_out_dir: str


_CANVAS_SIZE_REGEX = re.compile(r'(?P<width>.*), (?P<height>.*)',
                                re.MULTILINE | re.DOTALL)


def _get_canvas_size(test: Mapping[str, Any]):
    size = test.get('size', '100, 50')
    match = _CANVAS_SIZE_REGEX.match(size)
    if not match:
        raise InvalidTestDefinitionError(
            'Invalid canvas size "%s" in test %s. Expected a string matching '
            'this pattern: "%%s, %%s" %% (width, height)' %
            (size, test['name']))
    return match.group('width'), match.group('height')


def _write_reference_test(test: Mapping[str, Any],
                          jinja_env: jinja2.Environment,
                          template_params: MutableMapping[str, Any],
                          enabled_tests: Set[TestType],
                          canvas_path: str, offscreen_path: str):
    name = template_params["name"]
    js_ref = test.get('reference')
    html_ref = test.get('html_reference')
    if js_ref is not None and html_ref is not None:
        raise InvalidTestDefinitionError(
            f'Test {name} is invalid, "reference" and "html_reference" can\'t '
            'both be specified at the same time.')

    ref_params = template_params.copy()
    ref_params.update({'code': js_ref or html_ref})
    ref_template_name = 'reftest_element.html' if js_ref else 'reftest.html'
    if TestType.HTML_CANVAS in enabled_tests:
        pathlib.Path(f'{canvas_path}-expected.html').write_text(
            jinja_env.get_template(ref_template_name).render(ref_params),
            'utf-8')
    if {TestType.OFFSCREEN_CANVAS, TestType.WORKER} & enabled_tests:
        pathlib.Path(f'{offscreen_path}-expected.html').write_text(
            jinja_env.get_template(ref_template_name).render(ref_params),
            'utf-8')

    params = template_params.copy()
    params.update({
        'ref_link': f'{name}-expected.html',
        'fuzzy': test.get('fuzzy')
    })
    if TestType.HTML_CANVAS in enabled_tests:
        pathlib.Path(f'{canvas_path}.html').write_text(
            jinja_env.get_template("reftest_element.html").render(params),
            'utf-8')
    if TestType.OFFSCREEN_CANVAS in enabled_tests:
        pathlib.Path(f'{offscreen_path}.html').write_text(
            jinja_env.get_template("reftest_offscreen.html").render(params),
            'utf-8')
    if TestType.WORKER in enabled_tests:
        pathlib.Path(f'{offscreen_path}.w.html').write_text(
            jinja_env.get_template("reftest_worker.html").render(params),
            'utf-8')


def _write_testharness_test(jinja_env: jinja2.Environment,
                            template_params: MutableMapping[str, Any],
                            enabled_tests: Set[TestType],
                            canvas_path: str,
                            offscreen_path: str):
    # Create test cases for canvas and offscreencanvas.
    if TestType.HTML_CANVAS in enabled_tests:
        pathlib.Path(f'{canvas_path}.html').write_text(
            jinja_env.get_template("testharness_element.html").render(
                template_params), 'utf-8')

    template_params['done_needed'] = ('then(t_pass, t_fail);'
                                      not in template_params['code'])

    if TestType.OFFSCREEN_CANVAS in enabled_tests:
        pathlib.Path(f'{offscreen_path}.html').write_text(
            jinja_env.get_template("testharness_offscreen.html").render(
                template_params), 'utf-8')

    if TestType.WORKER in enabled_tests:
        pathlib.Path(f'{offscreen_path}.worker.js').write_text(
            jinja_env.get_template("testharness_worker.js").render(
                template_params), 'utf-8')


def _generate_test(test: Mapping[str, Any], jinja_env: jinja2.Environment,
                   sub_dir: str, enabled_tests: Set[TestType],
                   html_canvas_cfg: TestConfig,
                   offscreen_canvas_cfg: TestConfig) -> None:
    name = test['name']

    if test.get('expected', '') == 'green' and re.search(
            r'@assert pixel .* 0,0,0,0;', test['code']):
        print('Probable incorrect pixel test in %s' % name)

    expectation_html = ''
    if 'expected' in test and test['expected'] is not None:
        expected = test['expected']
        expected_img = None
        if expected == 'green':
            expected_img = '/images/green-100x50.png'
        elif expected == 'clear':
            expected_img = '/images/clear-100x50.png'
        else:
            if ';' in expected:
                print('Found semicolon in %s' % name)
            expected = re.sub(
                r'^size (\d+) (\d+)',
                r'surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, \1, \2)'
                r'\ncr = cairo.Context(surface)', expected)

            if TestType.HTML_CANVAS in enabled_tests:
                expected_canvas = (
                    expected + "\nsurface.write_to_png('%s.png')\n" %
                    os.path.join(html_canvas_cfg.image_out_dir, sub_dir, name))
                eval(compile(expected_canvas, '<test %s>' % name, 'exec'), {},
                    {'cairo': cairo})

            if {TestType.OFFSCREEN_CANVAS, TestType.WORKER} & enabled_tests:
                expected_offscreen = (
                    expected +
                    "\nsurface.write_to_png('%s.png')\n" % os.path.join(
                        offscreen_canvas_cfg.image_out_dir, sub_dir, name))
                eval(compile(expected_offscreen, '<test %s>' % name, 'exec'),
                     {}, {'cairo': cairo})

            expected_img = '%s.png' % name

        if expected_img:
            expectation_html = (
                '<p class="output expectedtext">Expected output:<p>'
                '<img src="%s" class="output expected" id="expected" '
                'alt="">' % expected_img)

    width, height = _get_canvas_size(test)

    images = ''
    for src in test.get('images', []):
        img_id = src.split('/')[-1]
        if '/' not in src:
            src = '../images/%s' % src
        images += '<img src="%s" id="%s" class="resource">\n' % (src, img_id)
    for src in test.get('svgimages', []):
        img_id = src.split('/')[-1]
        if '/' not in src:
            src = '../images/%s' % src
        images += ('<svg><image xlink:href="%s" id="%s" class="resource">'
                   '</svg>\n' % (src, img_id))
    images = images.replace('../images/', '/images/')

    is_promise_test = False
    if 'test_type' in test:
        if test['test_type'] == 'promise':
            is_promise_test = True
        else:
            raise InvalidTestDefinitionError(
                f'Test {name}\' test_type is invalid, it only accepts '
                '"promise" now for creating promise test type in the template '
                'file.')

    template_params = {
        'name': name,
        'desc': test.get('desc', ''),
        'notes': test.get('notes', ''),
        'images': images,
        'timeout': test.get('timeout'),
        'canvas': test.get('canvas', ''),
        'width': width,
        'height': height,
        'expected': expectation_html,
        'code': _expand_test_code(test['code']),
        'attributes': test.get('attributes', ''),
        'promise_test': is_promise_test
    }

    canvas_path = os.path.join(html_canvas_cfg.out_dir, sub_dir, name)
    offscreen_path = os.path.join(offscreen_canvas_cfg.out_dir, sub_dir, name)
    if 'manual' in test:
        canvas_path += '-manual'
        offscreen_path += '-manual'

    if 'reference' in test or 'html_reference' in test:
        _write_reference_test(test, jinja_env, template_params, enabled_tests,
                              canvas_path, offscreen_path)
    else:
        _write_testharness_test(jinja_env, template_params, enabled_tests,
                                canvas_path, offscreen_path)


def genTestUtils_union(NAME2DIRFILE: str) -> None:
    CANVASOUTPUTDIR = '../element'
    CANVASIMAGEOUTPUTDIR = '../element'
    OFFSCREENCANVASOUTPUTDIR = '../offscreen'
    OFFSCREENCANVASIMAGEOUTPUTDIR = '../offscreen'

    jinja_env = jinja2.Environment(
        loader=jinja2.PackageLoader("gentestutilsunion"),
        keep_trailing_newline=True,
        trim_blocks=True,
        lstrip_blocks=True)

    jinja_env.filters['double_quote_escape'] = _doubleQuoteEscape

    # Run with --test argument to run unit tests.
    if len(sys.argv) > 1 and sys.argv[1] == '--test':
        doctest = importlib.import_module('doctest')
        doctest.testmod()
        sys.exit()

    name_to_sub_dir = yaml.safe_load(pathlib.Path(NAME2DIRFILE).read_text())

    tests = []
    test_yaml_directory = 'yaml-new'
    TESTSFILES = [
        os.path.join(test_yaml_directory, f)
        for f in os.listdir(test_yaml_directory) if f.endswith('.yaml')
    ]
    for t in sum(
        [yaml.safe_load(pathlib.Path(f).read_text()) for f in TESTSFILES], []):
        if 'DISABLED' in t:
            continue
        if 'meta' in t:
            eval(compile(t['meta'], '<meta test>', 'exec'), {},
                 {'tests': tests})
        else:
            tests.append(t)

    # Ensure the test output directories exist.
    testdirs = [
        CANVASOUTPUTDIR, OFFSCREENCANVASOUTPUTDIR, CANVASIMAGEOUTPUTDIR,
        OFFSCREENCANVASIMAGEOUTPUTDIR
    ]
    for sub_dir in set(name_to_sub_dir.values()):
        testdirs.append('%s/%s' % (CANVASOUTPUTDIR, sub_dir))
        testdirs.append('%s/%s' % (OFFSCREENCANVASOUTPUTDIR, sub_dir))
    for d in testdirs:
        try:
            os.mkdir(d)
        except FileExistsError:
            pass  # Ignore if it already exists,

    used_tests = collections.defaultdict(set)
    for original_test in tests:
        variants = original_test.get('variants', {'': dict()})
        for variant_name, variant_params in variants.items():
            test = original_test.copy()
            if variant_name or variant_params:
                test['name'] += '.' + variant_name
                test['code'] = test['code'] % variant_params
                if 'reference' in test:
                    test['reference'] = test['reference'] % variant_params
                if 'html_reference' in test:
                    test['html_reference'] = (
                        test['html_reference'] % variant_params)
                test.update(variant_params)

            name = test['name']
            print('\r(%s)' % name, ' ' * 32, '\t')

            enabled_test_types = _get_enabled_test_types(test)

            already_tested = used_tests[name].intersection(
                enabled_test_types)
            if already_tested:
                raise InvalidTestDefinitionError(
                    f'Test {name} is defined twice for types {already_tested}')
            used_tests[name].update(enabled_test_types)

            sub_dir = _get_test_sub_dir(name, name_to_sub_dir)
            _generate_test(
                test,
                jinja_env,
                sub_dir,
                enabled_test_types,
                html_canvas_cfg=TestConfig(
                    out_dir=CANVASOUTPUTDIR,
                    image_out_dir=CANVASIMAGEOUTPUTDIR),
                offscreen_canvas_cfg=TestConfig(
                    out_dir=OFFSCREENCANVASOUTPUTDIR,
                    image_out_dir=OFFSCREENCANVASIMAGEOUTPUTDIR))

    print()
