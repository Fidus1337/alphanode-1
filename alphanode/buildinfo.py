"""Runtime read of the build identity written by packaging/make_build_stamp.py.

Glue module (not compiled): every consumer wants plain attribute access. The stamp ships as
a bundle resource; in a dev run it is absent and we return a 'dev' identity so nothing that
displays or reports the build has to special-case being un-frozen.
"""
import json
import os

from version import __version__

_HERE = os.path.dirname(os.path.abspath(__file__))
_cache = None


def _stamp_path():
    """The stamp sits beside this module — in the bundle that is _internal/, in dev alphanode/.
    apppaths is not imported here to keep buildinfo dependency-free for the node/CLI roles."""
    return os.path.join(_HERE, '_build_stamp.json')


def build_info():
    """{'version','build_id','git','built_at','vault_pub_fp'} — always populated. A dev run (no
    stamp) reports build_id 'dev'; a corrupt stamp is treated the same, never raised."""
    global _cache
    if _cache is not None:
        return _cache
    info = {'version': __version__, 'build_id': 'dev', 'git': 'dev',
            'built_at': None, 'vault_pub_fp': None, 'vault_url': None}
    try:
        with open(_stamp_path(), encoding='utf-8') as f:
            info.update({k: v for k, v in json.load(f).items() if k in info})
    except (OSError, ValueError):
        pass                                                # dev / missing / corrupt -> defaults
    _cache = info
    return info


def build_label():
    """One-line human id for titles, logs and the status page: 'v1.0.0 · build ab12… · git …'."""
    i = build_info()
    return f'v{i["version"]} · build {i["build_id"]} · git {i["git"]}'


def vault_url():
    """The hub URL baked into this build, or None (dev). The Windows .exe has no launcher to
    export ALPHANODE_VAULT_URL, so the stamp is where a cross-platform build carries it."""
    return build_info().get('vault_url')
