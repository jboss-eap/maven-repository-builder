import logging
import os
import re
import shutil
import zipfile

from maven_artifact import MavenArtifact


def generate_report(output, artifact_sources, artifact_list, report_name):
    """
    Generates report. The report consists of a summary page, groupId pages, artifactId pages and leaf artifact pages.
    Summary page contains list of roots, list of BOMs, list of multi-version artifacts, links to all artifacts and list
    of artifacts that do not match the BOM version. Each artifact has a separate page containing paths from roots to
    the artifact with path explanation.
    """
    multiversion_gas = dict()
    malformed_versions = dict()
    if os.path.exists(output):
        logging.warn("Target report path %s exists. Deleting...", output)
        shutil.rmtree(output)
    os.makedirs(os.path.join(output, "pages"))

    roots = []
    boms = set()
    for artifact_source in artifact_sources:
        if artifact_source["type"] == "dependency-graph":
            roots.extend(artifact_source['top-level-gavs'])
            boms = boms.union(artifact_source['injected-boms'])
    boms = sorted(list(boms))

    groupids = dict()
    version_pattern = re.compile("^.*[.-]redhat-[^.]+$")
    for ga in artifact_list:
        (groupid, artifactid) = ga.split(":")
        priority_list = artifact_list[ga]
        for priority in priority_list:
            versions = priority_list[priority]
            if versions:
                groupids.setdefault(groupid, dict()).setdefault(artifactid, dict()).update(versions)
                if len(groupids[groupid][artifactid]) > 1:
                    multiversion_gas.setdefault(groupid, dict())[artifactid] = groupids[groupid][artifactid]
                for version in versions:
                    if not version_pattern.match(version):
                        malformed_versions.setdefault(groupid, dict()).setdefault(artifactid, dict())[version] = groupids[groupid][artifactid][version]

    for groupid in groupids.keys():
        artifactids = groupids[groupid]
        for artifactid in artifactids.keys():
            versions = artifactids[artifactid]
            for version in versions.keys():
                art_spec = versions[version]

                ma = MavenArtifact.createFromGAV("%s:%s:%s" % (groupid, artifactid, version))
                generate_artifact_page(ma, roots, art_spec.paths, output, groupids)
            generate_artifactid_page(groupid, artifactid, versions, output)
        generate_groupid_page(groupid, artifactids, output)
    generate_summary(roots, boms, groupids, multiversion_gas, malformed_versions, output, report_name)
    generate_css(output)


def generate_artifact_page(ma, roots, paths, output, groupids):
    html = ("<html><head><title>Artifact {gav}</title>" + \
            "<link rel=\"stylesheet\" type=\"text/css\" href=\"style.css\"></head><body>" + \
            "<div class=\"header\"><a href=\"../index.html\">Back to repository summary</a></div>" + \
            "<div class=\"artifact\"><h1>{gav}</h1>" + \
            "<p class=\"breadcrumbs\"><a href=\"groupid_{groupid}.html\" title=\"GroupId {groupid}\">{groupid}</a>" + \
            "&nbsp;:&nbsp;<a href=\"artifactid_{groupid}${artifactid}.html\" title=\"ArtifactId {artifactid}\">{artifactid}</a>" + \
            "&nbsp;:&nbsp;{version}</p>" + \
            "<h2>Paths</h2><ul id=\"paths\">").format(gav=ma.getGAV().replace(":", " : "), groupid=ma.groupId, artifactid=ma.artifactId, version=ma.version)
    examples = ""
    if ma.getGAV() in roots:
        li = "<li>"
        li += "<a href=\"artifact_version_{gav_filename}.html\" title=\"{gav}\">{aid}</a>".format(
              gav=ma.getGAV().replace(":", " : "), aid=ma.artifactId, gav_filename=ma.getGAV().replace(":", "$"))
        li += " <span class=\"relation\">is root</span>"
        if ma.is_example():
            examples += li
        else:
            html += li

    for path in sorted(paths):
        rma = path[0].declaring
        li = "<li>"
        for rel in path:
            dec = rel.declaring
            if dec:
                rel_type = rel.rel_type
                if dec.groupId in groupids and dec.artifactId in groupids[dec.groupId] and dec.version in groupids[dec.groupId][dec.artifactId]:
                    li += "<a href=\"artifact_version_{gav_filename}.html\" title=\"{gav}\">{daid}</a>".format(
                          gav=dec.getGAV().replace(":", " : "), daid=dec.artifactId,
                          gav_filename=dec.getGAV().replace(":", "$"))
                else:
                    li += "<span class=\"excluded\" title=\"{gav} (excluded)\">{daid}</span>".format(
                          gav=dec.getGAV().replace(":", " : "), daid=dec.artifactId)
                li += " <span class=\"relation\">"
                if rel_type is None:
                    li += "unknown relation"
                elif rel_type == "DEPENDENCY":
                    if rel.extra == "embedded":
                        li += "embeds"
                    else:
                        li += "depends on (scope %s)" % rel.extra
                elif rel_type == "PARENT":
                    li += "has parent"
                elif rel_type == "PLUGIN":
                    li += "uses plugin"
                elif rel_type == "PLUGIN_DEP":
                    li += "uses plugin %s with added dependency" % rel.extra
                elif rel_type == "BOM":
                    li += "imports BOM"
                else:
                    li += "unknown relation (%s)" % rel_type
                li += "</span> "
            else:
                li += "... <span class=\"relation\">unknown relation</span> "
        leaf = path[-1].target
        gav = leaf.getGAV()
        li += "<a href=\"artifact_version_{gav_filename}.html\" title=\"{gav}\">{aid}</a></li>".format(
              gav=gav.replace(":", " : "), gav_filename=gav.replace(":", "$"), aid=leaf.artifactId)
        if rma.is_example():
            examples += li
        else:
            html += li
    html += examples.replace("<li>", "<li class=\"example\">")
    html += "</ul></div></body></html>"
    with open(os.path.join(output, "pages", "artifact_version_%s.html" % ma.getGAV().replace(":", "$")), "w") as htmlfile:
        htmlfile.write(html)


def generate_artifactid_page(groupid, artifactid, artifacts, output):
    html = ("<html><head><title>ArtifactId {groupid}:{artifactid}</title>" + \
            "<link rel=\"stylesheet\" type=\"text/css\" href=\"style.css\"></head><body>" + \
            "<div class=\"header\"><a href=\"../index.html\">Back to repository summary</a></div>" + \
            "<div class=\"artifact\"><h1>{groupid}:{artifactid}</h1>" + \
            "<p class=\"breadcrumbs\"><a href=\"groupid_{groupid}.html\" title=\"GroupId {groupid}\">{groupid}</a>" + \
            "&nbsp;:&nbsp;<a href=\"artifactid_{groupid}${artifactid}.html\" title=\"ArtifactId {artifactid}\">{artifactid}</a></p>" + \
            "<h2>Versions</h2><ul>").format(groupid=groupid, artifactid=artifactid)
    for version in sorted(artifacts.keys()):
        gav = "%s:%s:%s" % (groupid, artifactid, version)
        html += "<li><a href=\"artifact_version_{gav_filename}.html\">{version}</a></li>".format(
                version=version, gav_filename=gav.replace(":", "$"))
    html += "</ul></div></body></html>"
    with open(os.path.join(output, "pages",
                           "artifactid_{groupid}${artifactid}.html".format(groupid=groupid, artifactid=artifactid)
                           ), "w") as htmlfile:
        htmlfile.write(html)


def generate_groupid_page(groupid, artifactids, output):
    html = ("<html><head><title>GroupId {groupid}</title>" + \
            "<link rel=\"stylesheet\" type=\"text/css\" href=\"style.css\"></head><body>" + \
            "<div class=\"header\"><a href=\"../index.html\">Back to repository summary</a></div>" + \
            "<div class=\"artifact\"><h1>{groupid}</h1>" + \
            "<p class=\"breadcrumbs\"><a href=\"groupid_{groupid}.html\" title=\"GroupId {groupid}\">{groupid}</a></p>" + \
            "<h2>Artifacts</h2><ul>").format(groupid=groupid)
    for artifactid in sorted(artifactids.keys()):
        html += ("<li><a href=\"artifactid_{groupid}${artifactid}.html\" title=\"ArtifactId {artifactid}\">{artifactid}</a></li><ul>" + \
                 "").format(groupid=groupid, artifactid=artifactid)
        artifacts = artifactids[artifactid]
        for version in sorted(artifacts.keys()):
            gav = "%s:%s:%s" % (groupid, artifactid, version)
            html += ("<li><a href=\"artifact_version_{gav_filename}.html\">{ver}</a></li>" + \
                     "").format(ver=version, gav_filename=gav.replace(":", "$"))
        html += "</ul>"
    html += "</ul></div></body></html>"
    with open(os.path.join(output, "pages", "groupid_%s.html" % groupid), "w") as htmlfile:
        htmlfile.write(html)


def generate_summary(roots, boms, groupids, multiversion_gas, malformed_versions, output, report_name):
    html = ("<html><head><title>Repository {report_name}</title>" + \
            "<link rel=\"stylesheet\" type=\"text/css\" href=\"pages/style.css\"></head><body>" + \
            "<div class=\"artifact\"><h1>{report_name}</h1>" + \
            "").format(report_name=report_name)
    html += "<h2>Repo roots</h2><ul>"
    examples = ""
    for root in sorted(roots):
        ma = MavenArtifact.createFromGAV(root)
        gid = ma.groupId
        aid = ma.artifactId
        ver = ma.version
        if gid in groupids.keys() and aid in groupids[gid].keys() and ver in groupids[gid][aid]:
            if ma.is_example():
                examples += "<li class=\"error\"><a href=\"pages/artifact_version_{gid}${aid}${ver}.html\">{gid}&nbsp;:&nbsp;{aid}&nbsp;:&nbsp;{ver}</a></li>".format(
                            gid=gid, aid=aid, ver=ver)
            else:
                html += "<li><a href=\"pages/artifact_version_{gid}${aid}${ver}.html\">{gid}&nbsp;:&nbsp;{aid}&nbsp;:&nbsp;{ver}</a></li>".format(
                        gid=gid, aid=aid, ver=ver)
        else:
            if ma.is_example():
                examples += "<li class=\"example\">{gid}&nbsp;:&nbsp;{aid}&nbsp;:&nbsp;{ver}</li>".format(gid=gid, aid=aid, ver=ver)
            else:
                html += "<li class=\"error\">{gid}&nbsp;:&nbsp;{aid}&nbsp;:&nbsp;{ver}</li>".format(
                        gid=gid, aid=aid, ver=ver)
    html += examples + "</ul><h2>BOMs</h2><ul>"
    for bom in sorted(boms):
        ma = MavenArtifact.createFromGAV(bom)
        gid = ma.groupId
        aid = ma.artifactId
        ver = ma.version
        if gid in groupids.keys() and aid in groupids[gid].keys() and ver in groupids[gid][aid]:
            html += "<li><a href=\"pages/artifact_version_{gid}${aid}${ver}.html\">{gid}&nbsp;:&nbsp;{aid}&nbsp;:&nbsp;{ver}</a></li>".format(gid=gid, aid=aid, ver=ver)
        else:
            html += "<li><span class=\"error\">{gid}&nbsp;:&nbsp;{aid}&nbsp;:&nbsp;{ver}</span></li>".format(
                    gid=gid, aid=aid, ver=ver)
    html += "</ul><h2>Multi-versioned artifacts</h2><ul>"
    for groupid in sorted(multiversion_gas.keys()):
        html += ("<li><a href=\"pages/groupid_{groupid}.html\" title=\"GroupId {groupid}\">{groupid}</a></li><ul>" + \
                 "").format(groupid=groupid)
        artifactids = multiversion_gas[groupid]
        for artifactid in sorted(artifactids.keys()):
            html += ("<li><a href=\"pages/artifactid_{artifactid}.html\" title=\"ArtifactId {artifactid}\">{artifactid}</a></li><ul>" + \
                     "").format(artifactid=artifactid)
            artifacts = artifactids[artifactid]
            for version in sorted(artifacts.keys()):
                gav = "%s:%s:%s" % (groupid, artifactid, version)
                html += "<li><a href=\"pages/artifact_version_{gav_filename}.html\">{gav}</a></li>".format(
                        gav=gav, gav_filename=gav.replace(":", "$"))
            html += "</ul>"
        html += "</ul>"
    html += "</ul><h2>Malformed versions</h2><ul>"
    for groupid in sorted(malformed_versions.keys()):
        html += ("<li><a href=\"pages/groupid_{groupid}.html\" title=\"GroupId {groupid}\">{groupid}</a></li><ul>" + \
                 "").format(groupid=groupid)
        artifactids = malformed_versions[groupid]
        for artifactid in sorted(artifactids.keys()):
            html += ("<li><a href=\"pages/artifactid_{artifactid}.html\" title=\"ArtifactId {artifactid}\">{artifactid}</a></li><ul>" + \
                     "").format(artifactid=artifactid)
            artifacts = artifactids[artifactid]
            for version in sorted(artifacts.keys()):
                gav = "%s:%s:%s" % (groupid, artifactid, version)
                html += "<li><a href=\"pages/artifact_version_{gav_filename}.html\">{gav}</a></li>".format(
                        gav=gav, gav_filename=gav.replace(":", "$"))
            html += "</ul>"
        html += "</ul>"
    html += "</ul><h2>All artifacts</h2><ul>"
    for groupid in sorted(groupids.keys()):
        html += "<li><a href=\"pages/groupid_{groupid}.html\" title=\"GroupId {groupid}\">{groupid}</a></li><ul>".format(
                groupid=groupid)
        artifactids = groupids[groupid]
        for artifactid in sorted(artifactids.keys()):
            html += ("<li><a href=\"pages/artifactid_{groupid}${artifactid}.html\" title=\"ArtifactId {artifactid}\">{artifactid}</a></li><ul>" + \
                     "").format(groupid=groupid, artifactid=artifactid)
            artifacts = artifactids[artifactid]
            for version in sorted(artifacts.keys()):
                gav = "%s:%s:%s" % (groupid, artifactid, version)
                html += "<li><a href=\"pages/artifact_version_{gav_filename}.html\">{version}</a></li>".format(
                        version=version, gav_filename=gav.replace(":", "$"))
            html += "</ul>"
        html += "</ul>"
    html += "</ul></div></body></html>"
    with open(os.path.join(output, "index.html"), "w") as htmlfile:
        htmlfile.write(html)


def generate_css(output):
    css = ".error, .error a { color: red }\n.example, .example a { color: grey }\n.relation { color: grey; font-size: 0.8em }\n#paths li { padding-bottom: 0.5em }\n" \
          ".excluded { text-decoration: line-through }"
    with open(os.path.join(output, "pages", "style.css"), "w") as cssfile:
        cssfile.write(css)


def unzip(repository_zip, target_dir):
    if os.path.exists(target_dir):
        logging.warn("Target zip extract path %s exists. Deleting...", target_dir)
        shutil.rmtree(target_dir)
    zfile = zipfile.ZipFile(repository_zip)
    for name in zfile.namelist():
        _dirname = os.path.split(name)[0]
        dirname = os.path.join(target_dir, _dirname)
        logging.debug("Extracting %s into %s", name, dirname)
        zfile.extract(name, target_dir)
