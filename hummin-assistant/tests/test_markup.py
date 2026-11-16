import unittest

from hummin_assistant.markup import render


class MarkupTests(unittest.TestCase):
    def test_formatting(self):
        self.assertEqual(render("**b** and __c__ *i* _j_"), "<b>b</b> and <b>c</b> <i>i</i> <i>j</i>")
        self.assertEqual(render("use `a*b*c` now"), "use <tt>a*b*c</tt> now")
        self.assertEqual(render("```py\nx = 1 **2**\n<b>\n```"), "<pre>x = 1 **2**\n&lt;b&gt;</pre>")
        self.assertEqual(render("# T\n- a\n* b\n1. c"), "<b>T</b><br>\u2022 a<br>\u2022 b<br>1. c")
        self.assertEqual(render("see [d](https://x.io/a_b_c?q=1&r=2)"),
                         'see <a href="https://x.io/a_b_c?q=1&amp;r=2">d</a>')
        self.assertEqual(render("[`foo`](https://x.y)"), '<a href="https://x.y"><tt>foo</tt></a>')
        self.assertEqual(render("> note **b**\na > b"), "<i>│ note <b>b</b></i><br>a &gt; b")

    def test_injection_is_inert(self):
        self.assertEqual(render("<img src=http://x>"), "&lt;img src=http://x&gt;")
        self.assertEqual(render("![a](http://x)"), "a")
        self.assertEqual(render("[x](javascript:alert(1))"), "x")
        self.assertEqual(render('<a href="http://x">y</a>'), '&lt;a href="http://x"&gt;y&lt;/a&gt;')
        self.assertEqual(render('[x](https://a.b/"y)'), "x")
        out = render('[x](http://a" onclick="b)')
        self.assertNotIn("<a ", out.replace('<a href="http://a&quot;', ""))
        self.assertNotIn("\x00", render("a\x000\x00b"))
        self.assertNotIn("<a ", render("[x](https://a`b`c)"))

    def test_quotes_stay_literal(self):  # StyledText knows &amp; &lt; &gt; but maybe not &#x27;
        self.assertEqual(render("it's \"ok\""), "it's \"ok\"")

    def test_pathological_input_is_fast(self):
        import time
        for s in ("_a " * 20000, "*a " * 20000, "**a " * 20000, "[" * 20000, "[x](a" * 5000, "![" * 20000):
            t = time.monotonic()
            render(s)
            self.assertLess(time.monotonic() - t, 1.0, repr(s[:8]))


if __name__ == "__main__":
    unittest.main()
