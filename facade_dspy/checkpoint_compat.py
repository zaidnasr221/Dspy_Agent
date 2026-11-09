"""Known source revisions whose committed evidence can be reused unchanged."""


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
    'code:importer.py': (
        (
            '06ef809e82c50a5b04dcc621e5c50bbcf5ae4fda786f9ed95a643d97840ffe12',
            '59065c1dd533405b3cd693c9f6ab625234ad89ff8d6d4eb4c2521bdac13a9b14',
        ),
        (
            '747b3e96171d07cf8cd70130cc94f3aba5056e02f59ba506aba3e8a53aa220fd',
            'df410d0ec46e9a8119f1870a323661d7b797bae788c4518aebcd8ee2f3e98688',
        ),
    ),
    'code:opencode_tool.py': (
        (
            'bc8e1c440b717ad99df7864523852733571a29f98fd566669f6548546ae7793d',
        ),
        (
            '63ffc743827a8229d5f4de872b22e7c91942b5638a1f4dc61c96f96a1e1ef6d6',
            '71543d273243775bc0790c69414d5c8d46d87bbf64f41770643699b57f945c3f',
        ),
    ),
    'code:program.py': (
        (
            '5036e61d0293a5f78eef53c515f71d0566ec5419dc49ac41204cb3f6594f689f',
            '51b7ec8d17c012ed211aab3022167b42014a318685aef51eafaaf3a63de94d23',
            '7b77fd184d3ea86d20d91f9e414bbc9b329be4ac6984ea9e6757fc16f202d16c',
            '7c773cd95ff7fdda78e03e497dfb0ae27833ab32a5982f779eefdb7e705453b5',
            '7c7ae4d66d43eaada399ab1469eb8fa58961a75f9ce1c0287edab6d3b4f19cdb',
            '7d5322bc9e7a035a747ad748891780400ecbd3fcd7231c2ba367850253691788',
            'abe42dfcf9d96922e5d3ffe2f18748cef7f5f247705711a003b7f5ca8ba8d7ff',
            'df54d0ab856f7dff5ffdce3005f455c81c5e25af4b96d7478214e820bb74c754',
            'e33fa6fcd99cf282eefb1b256fe7bc0bbad443c4d2ecf8f0201cbb80dbae34c9',
        ),
        (
            '0da63f7e032846432a423d9987096e9233769d55baca372103c36f5eea97abac',
            '98d373b6c3e04d43519de83e03ad3e955fd1a0057be2ca43c2dcfbf24fabf3fc',
        ),
    ),
    'code:queue.py': (
        (
            '1310386dd79c65e80a6f1a389ba30ee2eddc8bfc4b4a2f8d3f9f9e6753ba36f0',
            '7aeab499bd345eda8223e645af4895c00207b4cbf8b5c4a5f3db403e947c4ca8',
            '99f8eeee2c4dad42c0675e814d749fcdd6c18921fddd0bd59d8f0e749620f7bb',
            'fc0c438384b67fa6d88f1dfdfe3462ecd80680f4e000bcb5a03d6c55dfcc509d',
        ),
        (
            '48be6c5655b529424164e50d7ad18038343e6917e750a5923cfd55fa0c3c20a5',
            'c881db8bb0e8af9d20b04840a4578aa46f9fa0b0e8640320aefa3c4afb637cbb',
        ),
    ),
    'code:schema.py': (
        (
            '3dc8cd2df6798767d14222c8d7bac8f97da5d77b1a65ecc5b091d68734df9b37',
            'e4b5431703e606b927a283d22cae7a36271936fad38cc6fdaab7cef0470a8138',
        ),
        (
            '9367b24e321ef5f4fe24a7ceaf9e7a68220567fa41075cc390227dae4744907f',
            '7274da35f2bddac61c7389d0c88e29af6d610ddb25da17f87879405fce91a87d',
        ),
    ),
    'code:source.py': (
        (
            '21cf4f96b937f2ae9efb1b0e1ce2b2ae96308c68f880bfb1482cb85486cf3bb6',
            'c64d747d02f6c62a515d4acdb205e5dfc9de27ff6726c565b11c9a1753e1ef6a',
        ),
        (
            '06b17abe684286458fcfb8ba9da42c6507419b552eb61124a9e4a1a7a8ea4c5e',
            'a03dc0ca6b77ab4d443c80f2c2d56f52f8810233adbebc205c0fb62e9a1bb348',
        ),
    ),
    'code:tool_protocol.py': (
        (
            'be676bddcc201bb80f307fcc3911f1c39d337887cfab2d541884f98ab4c53d40',
        ),
        (
            '38abc0836502782982ce67dbd57d5a20e0aa99a23eeb8d373f53de26e2dc9cde',
            'd90ac0785f87ce1f8505a20bb49ba456bb2189134f779c4fd823fae512e41837',
        ),
    ),
}


def compatible_code(ref, previous, current):
    if previous == current:
        return True
    upgrade = CHECKPOINT_UPGRADES.get(ref)
    return upgrade is not None and previous in upgrade[0] and current in upgrade[1]
