import unittest
from scripts.raccoon_service import patch_like

class RaccoonSafetyTests(unittest.TestCase):
    def test_instruction_patch_is_reviewed(self):
        self.assertTrue(patch_like("# Tempatkan tepat sebelum re.compile(pattern)"))
    def test_unified_diff_is_reviewed(self):
        self.assertTrue(patch_like("diff --git a/a.py b/a.py\n@@ -1 +1 @@"))
    def test_python_module_is_not_patch(self):
        self.assertFalse(patch_like("def hello():\n    return 'world'"))

if __name__ == "__main__": unittest.main()
