"""Known source revisions whose committed evidence can be reused unchanged.

Each entry maps earlier hashes of a tracked source file to its current hashes (LF and CRLF
checkouts). Completed checkpoints recorded against an earlier revision stay valid, so a code
change never silently re-runs paid extraction for an existing workspace.
"""


from hashlib import sha256
from pathlib import Path


CHECKPOINT_UPGRADES = {
    'code:budget.py': (
        (
            '75e60f96331d23bef4749b12aa1c46a9cce52dacd3382abf3003ad35cdb0b6cb',
        ),
        (
            'be6e53d460db4ad3f5031e3cc3077e582e73d1723758a4d2424b6fd63975411a',
            '4d85fca6c0eaf821612378be0e5d30e2ef11ebaa0829c6418c30163e43e39ec7',
        ),
    ),
    'code:claude_tool.py': (
        (
            '1354f0fc23912a919ad52c700c6e70780e7ca5d319a7f7fedd6face7ede231fd',
            '83fde2c00e020f74db79eed244d36e1e421dea034fb0047ed2d22da40a2b973c',
        ),
        (
            'c4062837d1c81004ed26bb12695822702922da9fc1141ef3bdfd14311def1938',
            '68bc293df2e3c345b72f2eb8758c8d9f1b5cd51ceb5f8fc799157e411c420696',
        ),
    ),
    'code:importer.py': (
        (
            '06ef809e82c50a5b04dcc621e5c50bbcf5ae4fda786f9ed95a643d97840ffe12',
            '59065c1dd533405b3cd693c9f6ab625234ad89ff8d6d4eb4c2521bdac13a9b14',
            '747b3e96171d07cf8cd70130cc94f3aba5056e02f59ba506aba3e8a53aa220fd',
            'df410d0ec46e9a8119f1870a323661d7b797bae788c4518aebcd8ee2f3e98688',
        ),
        (
            '8551709e6688b6b62acf2b9e412b555c9de8fc6ff2d9161a372e3e23045b5093',
            'd62d2f7e6f8650e3fc743333a56486ea70b9e14080fb972d335d8adec40cf148',
        ),
    ),
    'code:model.py': (
        (),
        (
            '8f6c4cd12f571674b0cf80bd3f204fedd887745c60cfc097cb70a3328a3c5a36',
            '3ca65f73aec53aa7612fc5c095305f76654c3964e766ec863d28700b56aa837d',
        ),
    ),
    'code:normalize.py': (
        (
            '4e58a9ee766568c44d7919377441d1f2dac7a09654b0cd1b9d1deb37900ffad8',
            'ef83d7d25ef58331747cc1933768d763be6170d22b81da5fda436f0bc405915f',
            '1d7e51a97622fdc529e346f50e361df9baba98b061aced4fe815e9a4d2e1a271',
            'b9bc04b53078123ca05923ddf4a02d6db91a6f7acf57780aa54d8a2ad40da501',
            '0b08ab65cd13854199a4b417b71325153908b052ab0516128adead1304c3ad73',
            '80a0235ac4ac5f3c28d8bc75020be434989d4560d12564738ea8d954dab8d58a',
        ),
        (
            'c0fc401bedc4185b3dc8fbff8f94dace6151718718826cdd047c3a6b700d9698',
            'd01fa79327dc56dd9854842a5d83c31917839f4bbbde2cae63149fed0bac0500',
        ),
    ),
    'code:opencode_tool.py': (
        (
            '63ffc743827a8229d5f4de872b22e7c91942b5638a1f4dc61c96f96a1e1ef6d6',
            '71543d273243775bc0790c69414d5c8d46d87bbf64f41770643699b57f945c3f',
            'bc8e1c440b717ad99df7864523852733571a29f98fd566669f6548546ae7793d',
        ),
        (
            'cb7e93c1ddd70cbfb086a0a5d6a12093c9824a84f0c4a72bfd673cb1e1dfb2b8',
            '30e2c6a6a45f51b2e783816611b75dba1f84e9cf2651e3c66461dcb05ff3dbc3',
        ),
    ),
    'code:program.py': (
        (
            '0da63f7e032846432a423d9987096e9233769d55baca372103c36f5eea97abac',
            '356e1bf6e07e5cde017aca01b3a7cafbc1070ed520af21afb9838d0b79f8608b',
            '39e54a65f7247842cf864c85c500cd4b31f75b200c5f3c0c8e9b56ea08270d4d',
            '4dfd4e4376006d1862fdfa9e4f149dedfd662c7e967437e1eb6229690ce9a069',
            '5036e61d0293a5f78eef53c515f71d0566ec5419dc49ac41204cb3f6594f689f',
            '51b7ec8d17c012ed211aab3022167b42014a318685aef51eafaaf3a63de94d23',
            '540e6e6d8ef4e24542ae7406e576e825f3cb1339ac82bb317eb867ebc3064144',
            '5fe917d145f1865ebc327bc4b026b2f8991f875031489585fd841c5d5f274677',
            '6f4f1cd2fc44c7983aee885322d2424652de9a3477a3624aec6107f72383ff7c',
            '7214615d09148ac9014c386f2abcbd1ef3b8fa5996b69d89db4083e58e4fad65',
            '7b77fd184d3ea86d20d91f9e414bbc9b329be4ac6984ea9e6757fc16f202d16c',
            '7c773cd95ff7fdda78e03e497dfb0ae27833ab32a5982f779eefdb7e705453b5',
            '7c7ae4d66d43eaada399ab1469eb8fa58961a75f9ce1c0287edab6d3b4f19cdb',
            '7d5322bc9e7a035a747ad748891780400ecbd3fcd7231c2ba367850253691788',
            '98d373b6c3e04d43519de83e03ad3e955fd1a0057be2ca43c2dcfbf24fabf3fc',
            'abe42dfcf9d96922e5d3ffe2f18748cef7f5f247705711a003b7f5ca8ba8d7ff',
            'b6a4d87b9a4966617cb3783f1584f808323a28bf7f7e3cbc01b8f6cdfaeab978',
            'c9b8212cec455a35f251f6f6024d6fe2430b200061999a264225ecc229f909ec',
            'df54d0ab856f7dff5ffdce3005f455c81c5e25af4b96d7478214e820bb74c754',
            'e33fa6fcd99cf282eefb1b256fe7bc0bbad443c4d2ecf8f0201cbb80dbae34c9',
            'fd36bb2e3cb4230e41f1aabf928225829bca1e6f8b0f80133b61ce91ff803459',
            '46deb204b875871b43da4e88f0d4f51ac14365f61d5b1daba9cde4e060521445',
            'f9da5a3315055c0d23ffd86925d88eb09950d5b4319c5b14bd141e2751b4a1a0',
            '531e702feff08cb76825eb738856311b4e5e759214e13be727a21da489d36c0c',
            'a53eafcaa95ee29be0d322e82df5d1cac37057d89d44a07a523b3bdbdff973d8',
            '14dc3efcf0d43aa5e36b85c793ac3c9cfb1f38c5e95341fde56b6ec11b5aa548',
            'f991030c874ffe5bac7c4e587503b23263f5b9079d548edd6efecfc76fc0ae4a',
            '8373493c5eb849d84bb4710df1f34ae8603a87ad5b65e9ec49245ada0b12a8b1',
            'befc279a15136cdba66edc1a68f47ef09e8050242938046dc01eaa102b96a3eb',
        ),
        (
            '2b87d88f84cf4a4dd3beee592f9df7403092978d6d903d1982cbcb948eb81edc',
            'a357b55714ae755b430164677e05b11cef8baa58b79e27ac5a9ae6b167ffc4cd',
        ),
    ),
    'code:queue.py': (
        (
            '1310386dd79c65e80a6f1a389ba30ee2eddc8bfc4b4a2f8d3f9f9e6753ba36f0',
            '48be6c5655b529424164e50d7ad18038343e6917e750a5923cfd55fa0c3c20a5',
            '7aeab499bd345eda8223e645af4895c00207b4cbf8b5c4a5f3db403e947c4ca8',
            '99f8eeee2c4dad42c0675e814d749fcdd6c18921fddd0bd59d8f0e749620f7bb',
            'c881db8bb0e8af9d20b04840a4578aa46f9fa0b0e8640320aefa3c4afb637cbb',
            'fc0c438384b67fa6d88f1dfdfe3462ecd80680f4e000bcb5a03d6c55dfcc509d',
            'd27545172168d1c6b9abbb95422255e2f7c89a67cb1c4a2d628dc718110e4098',
            'fcd8b05b64e35cc12ca52538e7d16d7e989b57d3cdd05206214c9112fb6db023',
        ),
        (
            'a9c508ca9c2b2d25892594dbb037c7f96b9674018b76e0fe76b027fe58d7d458',
            'e4e29c755055d01c80325484fabfb83f8faf5e4cbb6c48e7ad27c457a3fd29a7',
        ),
    ),
    'code:rules.py': (
        (
            '08885f2b1563d6d09de29beef0324dc749880a422dca10a04ed3b706b113aac9',
            '3d923b6eb9b9e021cc1d97fd498b1a83d4fb74d157966f24c059d87de1601a91',
            'e993ee048d8de76fd0c596805e84cf94d2741e5288799cbf7a0ca85ddd588ce3',
            'bc9857ef9d3a7b2bc6b979909924c1e7aa94f7794cec591e305952e23cc400b2',
            '82f2250dfd1d86df1c2ac048154d34c61242fa11ce03792afad1661d87c236c6',
            'fa8a11bddd02fe187b6bab871c9caa75aadd3cd7856fd1ccd86e38097b3b2015',
            '18ecd834afd3331beeee6aaca91d468c6aab88283bc7cad1a863ab10e57708b1',
            '19d0a9b2e8f9714d88bfb2aecb1c856729c388e176d3e1441fe0af3676fe9db1',
            'fbe6124a12814b91c50d63b7722f4ee800fe55885eb9d06b744787637787d96f',
            '1a71353dd571517675d8d11456e1ee05c322b30edfa2544135e4768db3cb539a',
            '970b5d835e1f411d3ca4e0a0b575d3bf29647b50e9fef976baa21940775043e2',
            '3d031857ba1df765501c66d07f21f286d525e595f9ae64e96a6bacd517da42b3',
        ),
        (
            '9bdcd79cfb2ef7eafd94ab9f9f9d6123dad7f6d81170afcea15f30ecf476432a',
            'fdd60163e8222c47f2da9803a3d2760f4d3a2010a74f514eaae7b92728d6cc1e',
        ),
    ),
    'code:schema.py': (
        (
            '3c041b71f3ea7ec765bacd982e94c8960c61784f357635e0e22db0c74ebc509d',
            '3dc8cd2df6798767d14222c8d7bac8f97da5d77b1a65ecc5b091d68734df9b37',
            '7274da35f2bddac61c7389d0c88e29af6d610ddb25da17f87879405fce91a87d',
            '9367b24e321ef5f4fe24a7ceaf9e7a68220567fa41075cc390227dae4744907f',
            'c5c64f2e2f7e701de716c74eca09e5223735a9a94009413a7670f0b8f89bca67',
            'e4b5431703e606b927a283d22cae7a36271936fad38cc6fdaab7cef0470a8138',
            'f0a04e3d3bd1b4b958a23bcc131b0a3fdbd8fbac7257bde6b273f3cc0518b507',
            '4e2fb5eabc52d8476de8912e8974dfe98be8f983cee0018b1ec31f2486102877',
            'fd5d60d78e15f42dc7e362983dee6fd164ee5520b01d7cd3343b939ef1d23268',
            '29e152f1e0a9b1db03242126369e93ac627fdc5d001d2a5ec220f8a525f65fb2',
            '537d9c2411d3b4137bca554390464afc46dc3e29fec66f56d050deaaad24769d',
            '333c4e81fdb41682fe2b1ba3328dc4836b36da6a861c7c0ded3681a3f06628c3',
            'db823d28756b9d83cf5481b7435794fb76a1771a44a81513a73f1ef2e7aeb56a',
            '8e2ca0a0c4c162cd97ecc055228629cdc296c394f69d9e1603446f4178251a11',
            'e274eef478b2d9590e93231a98897558345a0174c676b7bbdb2f1014fb821a7e',
            '50e6791ac2876c7b9b927b7c7510b07bad461ce5c12f8e81416e6b47f94676bf',
            '95d566e88bae5e9a76d86dabf82902f9926d048370f8b871c47467331b04c44e',
            '2af9c150dd4d4e19f690a20e2cefa26b0ddfdff8789b4b0f9fafec23b9361214',
            '732850af9a3d88109c36b0cdf5739b955f76faa9cd08902b5973767ad3159e9d',
            '4d3002914c6aaf78c574c5a3ede61f6d273af2daee14fe4ec27ad4c33086a58e',
            '5a4917d6f0d5fa84bcd3450031f605014458ba61135ac07758e8d51c61c1f611',
        ),
        (
            '3736f78375e5b340a6211baf10d750a7f7341ab2fe5f12219bcbd2d610cefda6',
            '469c4642bce36d924027c4bfc8cc53f4917a5bce01bc11eabdf8b5ab599b4453',
        ),
    ),
    'code:source.py': (
        (
            '06b17abe684286458fcfb8ba9da42c6507419b552eb61124a9e4a1a7a8ea4c5e',
            '21cf4f96b937f2ae9efb1b0e1ce2b2ae96308c68f880bfb1482cb85486cf3bb6',
            '44375513d893b05526daa4e171f698116ac482cf6413bb676e4345232f9f528c',
            'a03dc0ca6b77ab4d443c80f2c2d56f52f8810233adbebc205c0fb62e9a1bb348',
            'c64d747d02f6c62a515d4acdb205e5dfc9de27ff6726c565b11c9a1753e1ef6a',
            'ef739682787ab9937d46daa06225a09382c07c8ed359201f021e0f0c243375fc',
        ),
        (
            'e7f3a68996bf64804e0296243cbf972f91ca6911c6a9964996b716d292c4b1ad',
            '68b50197f94b8a80af865054d6f1cce69d046d702817e1e7a85517bb6d9d7d75',
        ),
    ),
    'code:tool_protocol.py': (
        (
            '38abc0836502782982ce67dbd57d5a20e0aa99a23eeb8d373f53de26e2dc9cde',
            'be676bddcc201bb80f307fcc3911f1c39d337887cfab2d541884f98ab4c53d40',
            'd90ac0785f87ce1f8505a20bb49ba456bb2189134f779c4fd823fae512e41837',
        ),
        (
            '7c710b5be15bacad8a88ab481fca5628ba4ef04e8b32af2ead8913b461b5658f',
            '492a5d78ccf0ea4253084e7ca021597bc60b21609ec26236c2c26d75496acd3b',
        ),
    ),
    'code:knowledge/system_profiles.json': (
        (
            '8decc6ddb9115ac2f4d40626d2635229a3f7fc651a7f0645fded13acad18dd53',
            '20645fd1b70266bdbdefdf0bc042f0807e8957778f7974b2de7ccde0687f4d89',
            'e14b7f28d083f7b4e9e8268a062a0ba89a51113e2b5f50ae74825f4c0fcdf505',
            '8a5f19e595671c5a4c60dac11953e543ca53ba7aa6609215dfdb29cce5e26f7f',
            'fb1fa034b0d2ca8aa05c29b761dbe7e06de9eb693a0597df3a73f31a3a69efdd',
            'f65b1b7265348727fa1e390dbcbb3cc098577e1f4d4a8c8dcf3280e0acc4c519',
        ),
        (
            'ae4cf1ce52ea902e42b06187ad735b6b017471e9db02686e4759c9d1501db2d2',
            '5dcc3abd1cd25ab28ea2d603ac63295319e2f8a02c0cf066fd150a23e6866f6c',
        ),
    ),
}


def compatible_code(ref, previous, current):
    if previous == current:
        return True
    if ref.startswith('code:'):
        root = Path(__file__).resolve().parent
        local = (root / ref[5:]).resolve()
        if local.is_relative_to(root) and local.is_file():
            normalized = local.read_bytes().replace(b'\r\n', b'\n')
            hashes = {sha256(value).hexdigest() for value in (normalized, normalized.replace(b'\n', b'\r\n'))}
            if previous in hashes and current in hashes:
                return True
    upgrade = CHECKPOINT_UPGRADES.get(ref)
    return upgrade is not None and current in upgrade[1] and (
        previous in upgrade[0] or previous in upgrade[1])
