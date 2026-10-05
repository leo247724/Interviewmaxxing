"""Exercise the actual text-entry JavaScript without a browser or profile I/O."""
import ast
import json
import shutil
import subprocess
from pathlib import Path

import pytest

DRIVER = Path(__file__).resolve().parents[2] / '.imx/dynamic-applications/real-chrome/rc.py'


@pytest.mark.parametrize(('tag', 'kind', 'text', 'expected'), [
    ('INPUT', 'text', 'One paragraph.\n\nAnother paragraph.', 'One paragraph. Another paragraph.'),
    ('INPUT', 'text', 'First\r\nSecond', 'First Second'),
    ('INPUT', 'text', 'Same single line.', 'Same single line.'),
    ('TEXTAREA', 'textarea', 'First\n\nSecond', 'First\n\nSecond'),
])
def test_actual_js_preserves_words_and_textarea_paragraphs(tag, kind, text, expected):
    node = shutil.which('node')
    if not node or not DRIVER.exists():
        pytest.skip('Local driver and Node are required for offline JavaScript check')
    tree = ast.parse(DRIVER.read_text())
    script = next(ast.literal_eval(n.value) for n in tree.body
                  if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'SET_TEXT' for t in n.targets))
    fixture = json.dumps({'tag': tag, 'kind': kind})
    prefix = '''const f=FIXTURE;
class Element {
 constructor(){this.tagName=f.tag;this.type=f.kind;this._value='';}
 get value(){return this._value;}
 set value(v){this._value=this.tagName==='INPUT'?v.replace(/[\\r\\n]/g,''):v;}
 focus(){} blur(){} dispatchEvent(){}
}
const el=new Element(); const document={querySelector:()=>el};
const HTMLInputElement=Element; class Event {constructor(){}}
'''.replace('FIXTURE', fixture)
    js = script % json.dumps({'sel': '#answer', 'text': text})
    program = prefix + 'const result=JSON.parse(' + js + '); console.log(JSON.stringify({result,value:el.value}));'
    output = subprocess.run([node, '-e', program], capture_output=True, text=True, check=True)
    observed = json.loads(output.stdout)
    assert observed['result']['ok'] is True
    assert observed['value'] == expected
    assert observed['result']['linebreaks_normalized'] == (text != expected)
