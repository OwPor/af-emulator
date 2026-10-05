import ast
from pathlib import Path
import unittest

ROOT=Path(__file__).resolve().parents[1]

class LoaderNicknameTests(unittest.TestCase):
    def test_owner_only_rename_is_guarded_by_shared_login_mode(self):
        text=(ROOT/'tools/server_spawner/AFDevLoader_v48_spawner_multi_instance.py').read_text()
        tree=ast.parse(text)
        calls=[n for n in ast.walk(tree) if isinstance(n,ast.Call) and isinstance(n.func,ast.Name) and n.func.id=='r20_sync_remote_pri_name']
        self.assertEqual(len(calls),1)
        parents={child:parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
        node=calls[0]
        while node in parents:
            node=parents[node]
            if isinstance(node,ast.If) and ast.unparse(node.test)=="os.environ.get('AF_DYNAMIC_LOGIN_NAMES') != '1'":break
        else:self.fail('Owner-only nickname sync is not guarded')
        bridge=(ROOT/'tools/bridge/af_ds_udp_bridge_v9_multi_peer_latch.py').read_text()
        self.assertIn('os.environ["AF_DYNAMIC_LOGIN_NAMES"] = "1"',bridge)

if __name__=='__main__':unittest.main()
