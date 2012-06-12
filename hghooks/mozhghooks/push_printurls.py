#!/usr/bin/python
import os.path
from mercurial.node import short

hgNameToRevURL = {
    'comm-central'    : 'comm-central/',
    'fx-team'         : 'integration/fx-team/',
    'mozilla-central' : 'mozilla-central/',
    'mozilla-inbound' : 'integration/mozilla-inbound/',
    'try'             : 'try/',
    'mozilla-aurora'  : 'releases/mozilla-aurora/',
    'mozilla-beta'    : 'releases/mozilla-beta/',
    'mozilla-release' : 'releases/mozilla-release/',
    'mozilla-esr10'   : 'releases/mozilla-esr10/',
    'comm-aurora'     : 'releases/comm-aurora/',
    'comm-beta'       : 'releases/comm-beta/',
    'comm-release'    : 'releases/comm-release/',
    'comm-esr10'      : 'releases/comm-esr10/',
}


def hook(ui, repo, node, hooktype, **kwargs):
    repo_name = os.path.basename(repo.root)
    if repo_name not in hgNameToRevURL:
        return 0

    # All changesets from node to "tip" inclusive are part of this push.
    rev = repo.changectx(node).rev()
    tip = repo.changectx('tip').rev()

    num_changes = tip + 1 - rev
    url = 'https://hg.mozilla.org/' + hgNameToRevURL[repo_name]

    if num_changes <= 10:
        plural = 's' if num_changes > 1 else ''
        print 'You can view your change%s at the following URL%s:' % (plural, plural)

        for i in xrange(rev, tip + 1):
            node = short(repo.changectx(i).node())
            print '  %srev/%s' % (url, node)
    else:
       tip_node = short(repo.changectx(tip).node())
       print 'You can view the pushlog for your changes at the following URL:'
       print '  %spushloghtml?changeset=%s' % (url, tip_node)

    return 0
