import copy
import os
import re
import logging
import traceback
from carto_client import CartoClient
import multiprocessing.pool
from multiprocessing.pool import ThreadPool
from multiprocessing import Lock
from multiprocessing import Queue
from subprocess import Popen
from subprocess import PIPE
import requests

import maven_repo_util
from maven_artifact import MavenArtifact
import time


class ArtifactListBuilder:
    """
    Class loading artifact "list" from sources defined in the given
    configuration. The result is dictionary with following structure:

    "<groupId>:<artifactId>" (string)
      L <artifact source priority> (int)
         L <version> (string)
            L artifact specification (repo url string and list of types with found classifiers)
    """

    SETTINGS_TPL = """
        <settings>
          <localRepository>${temp}.m2/repository</localRepository>
          <mirrors>
            <mirror>
              <id>maven-repo-builder-override</id>
              <mirrorOf>*</mirrorOf>
              <url>${url}</url>
            </mirror>
          </mirrors>
        </settings>"""

    notMainExtClassifiers = set(["pom:", "jar:javadoc", "jar:sources", "jar:tests", "jar:test-sources",
                                 "tar.gz:project-sources", "xml:site", "zip:patches",
                                 "zip:scm-sources", "zip:source-release"])

    IGNORED_REPOSITORY_FILES = set(["maven-metadata.xml", "maven-metadata.xml.md5", "maven-metadata.xml.sha1",
                                    "maven-metadata.xml.sha256"])

    MAX_THREADS_DICT = {"mead-tag": 2, "dependency-list": 1, "dependency-graph": 6, "repository": 2}

    def __init__(self, configuration):
        self.configuration = configuration
        self.errors = Queue()
        self.results_lock = Lock()
        self.results = {}
        self.max_threads = 6

    def buildList(self):
        """
        Build the artifact "list" from sources defined in the given configuration.

        :returns: Dictionary described above.
        """
        priority = 0
        pool_dict = {}

        for source in self.configuration.artifactSources:
            priority += 1
            pool = pool_dict.setdefault(source['type'], ThreadPool(self.MAX_THREADS_DICT[source['type']]))
            pool.apply_async(self._read_artifact_source, args=[source, priority],
                             callback=self._add_result)

        for pool in pool_dict.values():
            pool.close()

        at_least_1_runs = True
        all_keys = range(1, len(self.configuration.artifactSources) + 1)
        finished = False
        while at_least_1_runs:
            for i in range(30):
                time.sleep(1)

                if not self.errors.empty():
                    for pool in pool_dict.values():
                        logging.debug("Terminating pool %s", str(pool))
                        pool.terminate()
                    finished = True
                    break

            at_least_1_runs = False
            if not finished:
                self.results_lock.acquire()
                finished = sorted(list(self.results.keys()))
                self.results_lock.release()
                if all_keys != finished:
                    logging.debug("Still waiting for priorities %s to finish", str(list(set(all_keys) - set(finished))))
                    at_least_1_runs = True

        for pool in pool_dict.values():
            if pool._state != multiprocessing.pool.TERMINATE:
                pool.join()

        if not self.errors.empty():
            raise RuntimeError("%i error(s) occured during reading of artifact list." % self.errors.qsize())

        return self._get_artifact_list()

    def _add_result(self, result):
        if result:
            try:
                self.results_lock.acquire()
                self.results.update(result)
            finally:
                self.results_lock.release()

    def _get_artifact_list(self):
        artifactList = {}
        for priority, artifacts in self.results.iteritems():
            logging.debug("Placing %d artifacts in the result list", len(artifacts))
            for artifact in artifacts:
                ga = artifact.getGA()
                artSpec = artifacts[artifact]
                artifactList.setdefault(ga, {}).setdefault(priority, {})
                if artifact.version in artifactList[ga][priority]:
                    artifactList[ga][priority][artifact.version].merge(artSpec)
                else:
                    artifactList[ga][priority][artifact.version] = artSpec
            logging.debug("The result contains %d GAs so far", len(artifactList))

        return artifactList

    def _read_artifact_source(self, source, priority):
        """
        Reads artifact list from the given artifact source.

        :param source: artifact source configuration
        :param priority: priority of the given source
        :returns: artifact list read from the source
        """
        try:
            if source['type'] == 'mead-tag':
                logging.info("Building artifact list from tag %s", source['tag-name'])
                artifacts = self._listMeadTagArtifacts(source['koji-url'],
                                                       source['download-root-url'],
                                                       source['tag-name'],
                                                       source['included-gav-patterns'])
            elif source['type'] == 'dependency-list':
                logging.info("Building artifact list from top level list of GAVs")
                artifacts = self._listDependencies(source['repo-url'],
                                                   self._parseDepList(source['top-level-gavs']),
                                                   source['recursive'],
                                                   source['include-scope'],
                                                   source['skip-missing'])
            elif source['type'] == 'dependency-graph':
                logging.info("Building artifact list from dependency graph of top level GAVs")
                artifacts = self._listDependencyGraph(source['carto-url'],
                                                      source['wsid'],
                                                      source['source-key'],
                                                      self._parseDepList(source['top-level-gavs']),
                                                      source['excluded-sources'],
                                                      source['excluded-subgraphs'],
                                                      source['preset'],
                                                      source['mutator'],
                                                      source['patcher-ids'],
                                                      source['injected-boms'],
                                                      self.configuration.analyze)
            elif source['type'] == 'repository':
                logging.info("Building artifact list from repository %s", source['repo-url'])
                artifacts = self._listRepository(source['repo-url'],
                                                 source['included-gav-patterns'],
                                                 source['included-gatcvs'])
            else:
                logging.warning("Unsupported source type: %s", source['type'])
                return

            if source["excludedGAVs"]:
                self._filterExcludedGAVs(artifacts, source["excludedGAVs"], priority)

            return {priority: artifacts}
        except BaseException as ex:
            tb = traceback.format_exc()
            logging.error("Error while reading artifacts in priority %i: %s. Traceback\n%s", priority, ex, tb)
            self.errors.put(ex)
            raise ex

    def _filterExcludedGAVs(self, artifacts, excludedGAVs, priority):
        """
        Filter artifactList removing specified GAVs.

        :param artifacts: artifact list to be filtered.
        :param excludedGAVs: list of excluded GAVs patterns
        :param priority: artifact source priority
        :returns: artifact list without artifacts that matched specified GAVs.
        """
        logging.debug("Filtering excluded GAVs from partial result (priority %i).", priority)
        regExps = maven_repo_util.getRegExpsFromStrings(excludedGAVs)
        gavRegExps = []
        gatcvRegExps = []
        for regExp in regExps:
            if regExp.pattern.count(":") > 2:
                gatcvRegExps.append(regExp)
            else:
                gavRegExps.append(regExp)
        for artifact in copy.copy(artifacts):
            gav = artifact.getGAV()
            artSpec = artifacts[artifact]
            if maven_repo_util.somethingMatch(gavRegExps, gav):
                del artifacts[artifact]
            else:
                for artType in copy.deepcopy(artSpec.artTypes.keys()):
                    at = artSpec.artTypes[artType]
                    for classifier in copy.deepcopy(at.classifiers):
                        ga = artifact.getGA()
                        if classifier:
                            gatcv = "%s:%s:%s:%s" % (ga, artType, classifier, artifact.version)
                        else:
                            gatcv = "%s:%s:%s" % (ga, artType, artifact.version)
                        if maven_repo_util.somethingMatch(gatcvRegExps, gatcv):
                            logging.debug("Dropping GATCV %s because it matches an excluded GAV pattern.", gatcv)
                            at.classifiers.remove(classifier)
                    if not at.classifiers:
                        logging.debug("Dropping GATV %s:%s:%s because of no classifiers left.", ga, artType,
                                      artifact.version)
                        del artSpec.artTypes[artType]
                if not artSpec.containsMain():
                    logging.debug("Dropping GAV %s because of no main artifact left.", gav)
                    del artifacts[artifact]
        return artifacts

    def _listMeadTagArtifacts(self, kojiUrl, downloadRootUrl, tagName, gavPatterns):
        """
        Loads maven artifacts from koji (brew/mead).

        :param kojiUrl: Koji/Brew/Mead URL
        :param downloadRootUrl: Download root URL of the artifacts
        :param tagName: Koji/Brew/Mead tag name
        :returns: Dictionary where index is MavenArtifact object and value is ArtifactSpec with its
                  repo root URL.
        """
        import koji

        kojiSession = koji.ClientSession(kojiUrl)
        logging.debug("Getting latest maven artifacts from tag %s.", tagName)
        kojiArtifacts = kojiSession.getLatestMavenArchives(tagName)

        filenameDict = {}
        for artifact in kojiArtifacts:
            groupId = artifact['group_id']
            artifactId = artifact['artifact_id']
            version = artifact['version']
            gavUrl = "%s%s/%s/%s/maven/" % (maven_repo_util.slashAtTheEnd(downloadRootUrl), artifact['build_name'],
                                            artifact['build_version'], artifact['build_release'])
            gavu = (groupId, artifactId, version, gavUrl)
            filename = artifact['filename']
            filenameDict.setdefault(gavu, []).append(filename)

        gavuExtClass = {}  # { (g,a,v,url): {ext: set([class])} }
        suffixes = {}      # { (g,a,v,url): suffix }

        for gavu in filenameDict:
            artifactId = gavu[1]
            version = gavu[2]
            filenames = filenameDict[gavu]
            (extsAndClass, suffix) = self._getExtensionsAndClassifiers(artifactId, version, filenames)

            if extsAndClass:
                gavuExtClass[gavu] = {}
                self._updateExtensionsAndClassifiers(gavuExtClass[gavu], extsAndClass)

                if suffix is not None:
                    suffixes[gavu] = suffix

        artifacts = {}
        for gavu in gavuExtClass:
            self._addArtifact(artifacts, gavu[0], gavu[1], gavu[2], gavuExtClass[gavu], suffixes.get(gavu), gavu[3])

        if gavPatterns:
            logging.debug("Filtering artifacts contained in the tag by GAV patterns list.")
        return self._filterArtifactsByPatterns(artifacts, gavPatterns, None)

    def _listDependencies(self, repoUrls, gavs, recursive, include_scope, skipmissing):
        """
        Loads maven artifacts from mvn dependency:list.

        :param repoUrls: URL of the repositories that contains the listed artifacts
        :param gavs: List of top level GAVs
        :param recursive: runs dependency:list recursively using the previously discovered dependencies if True
        :param include_scope: defines scope which will be used when running mvn as includeScope parameter, can be None
                              to use Maven's default
        :returns: Dictionary where index is MavenArtifact object and value is
                  ArtifactSpec with its repo root URL
        """
        artifacts = {}
        workingSet = set(gavs)
        checkedSet = set()

        while workingSet:
            gav = workingSet.pop()
            checkedSet.add(gav)
            logging.debug("Resolving dependencies for %s", gav)
            artifact = MavenArtifact.createFromGAV(gav)

            pomFilename = 'poms/' + artifact.getPomFilename()
            successPomUrl = None
            fetched = False
            for repoUrl in repoUrls:
                pomUrl = maven_repo_util.slashAtTheEnd(repoUrl) + artifact.getPomFilepath()
                fetched = maven_repo_util.fetchFile(pomUrl, pomFilename)
                if fetched:
                    successPomUrl = repoUrl
                    break

            if not fetched:
                logging.warning("Failed to retrieve pom file for artifact %s", gav)
                continue

            tempDir = maven_repo_util.getTempDir()
            if not os.path.exists(tempDir):
                os.makedirs(tempDir)

            # Create settings.xml
            settingsFile = tempDir + "settings.xml"
            settingsContent = self.SETTINGS_TPL.replace('${url}', successPomUrl) \
                                               .replace('${temp}', maven_repo_util.getTempDir())
            with open(settingsFile, 'w') as settings:
                settings.write(settingsContent)

            # Build dependency:list
            depsDir = tempDir + "maven-deps-output/"
            outFile = depsDir + gav + ".out"
            args = ['mvn', 'dependency:list', '-N',
                                              '-DoutputFile=' + outFile,
                                              '-f', pomFilename,
                                              '-s', settingsFile]
            if include_scope:
                args.append("-DincludeScope=%s" % include_scope)
            logging.debug("Running Maven:\n  %s", " ".join(args))
            logging.debug("settings.xml contents: %s", settingsContent)
            mvn = Popen(args, stdout=PIPE)
            mvnStdout = mvn.communicate()[0]
            logging.debug("Maven output:\n%s", mvnStdout)

            if mvn.returncode != 0:
                logging.warning("Maven failed to finish with success. Skipping artifact %s", gav)
                continue

            with open(outFile, 'r') as out:
                depLines = out.readlines()
            gavList = self._parseDepList(depLines)
            logging.debug("Resolved dependencies of %s: %s", gav, str(gavList))

            newArtifacts = self._listArtifacts(repoUrls, gavList)

            if recursive:
                for artifact in newArtifacts:
                    ngav = artifact.getGAV()
                    if ngav not in checkedSet:
                        workingSet.add(ngav)

            if self.configuration.isAllClassifiers():
                resultingArtifacts = {}
                for artifact in newArtifacts.keys():
                    spec = newArtifacts[artifact]
                    try:
                        out = self._lftpFind(spec.url + artifact.getDirPath())
                    except IOError as ex:
                        if skipmissing:
                            logging.warn("Error while listing files in %s: %s. Skipping...",
                                         spec.url + artifact.getDirPath(), str(ex))
                            continue
                        else:
                            raise ex

                    files = []
                    for line in out.split('\n'):
                        if line != "./" and line != "":
                            files.append(line[2:])

                    (extsAndClass, suffix) = self._getExtensionsAndClassifiers(
                        artifact.artifactId, artifact.version, files)
                    if artifact.artifactType in extsAndClass:
                        self._addArtifact(resultingArtifacts, artifact.groupId, artifact.artifactId,
                                          artifact.version, extsAndClass, suffix, spec.url)
                    else:
                        if files:
                            logging.warn("Main artifact (%s) is missing in filelist listed from %s. Files were:\n%s",
                                         artifact.artifactType, spec.url + artifact.getDirPath(), "\n".join(files))
                        else:
                            logging.warn("An empty filelist was listed from %s. Skipping...",
                                         spec.url + artifact.getDirPath())
                newArtifacts = resultingArtifacts

            artifacts.update(newArtifacts)

        return artifacts

    def _listDependencyGraph(self, cartoUrl, wsid, sourceKey, gavs, excludedSources=[], excludedSubgraphs=[],
                             preset="requires", mutator=None, patcherIds=[], injectedBOMs=[], analyze=False):
        """
        Loads maven artifacts from dependency graph.

        :param cartoUrl: URL of the Cartographer instance
        :param wsid: workspace ID
        :param sourceKey: the Cartographer artifact source key, consisting of the source type and
                          its name of the form <{repository|deploy|group}:<name>>
        :param gavs: List of top level GAVs
        :param excludedSources: list of excluded sources' keys
        :param excludedSubgraphs: list of artifacts' GAVs which we want to exclude along with their subgraphs
        :param preset: preset used while creating the urlmap
        :param mutator: mutator used for dependency graph mutation
        :param patcherIds: list of patcher ID strings for Cartographer
        :param injectedBOMs: list of injected BOMs used with dependency management injection
                             Maven extension
        :returns: Dictionary where index is MavenArtifact object and value is
                  ArtifactSpec with its repo root URL
        """
        carto = CartoClient(cartoUrl)

        if not preset:
            preset = "sob-build"  # only runtime dependencies

        if analyze and not wsid:
            _wsid = "temp"
        else:
            _wsid = wsid

        # Resolve graph MANIFEST for GAVs
        if self.configuration.useCache:
            urlmap = carto.urlmap(_wsid, sourceKey, gavs, self.configuration.addClassifiers, excludedSources,
                                  excludedSubgraphs, preset, mutator, patcherIds, injectedBOMs)
        else:
            urlmap = carto.urlmap_nocache(_wsid, sourceKey, gavs, self.configuration.addClassifiers, excludedSources,
                                          excludedSubgraphs, preset, mutator, patcherIds, injectedBOMs)

        # parse returned map
        artifacts = {}
        if "projects" in urlmap:
            urlmap = urlmap["projects"]
        for gav in urlmap:
            artifact = MavenArtifact.createFromGAV(gav)
            groupId = artifact.groupId
            artifactId = artifact.artifactId
            version = artifact.version

            filenames = urlmap[gav]["files"]
            url = urlmap[gav]["repoUrl"]

            (extsAndClass, suffix) = self._getExtensionsAndClassifiers(artifactId, version, filenames)

            self._addArtifact(artifacts, groupId, artifactId, version, extsAndClass, suffix, url)

        if analyze:
            gas = []
            for ma in artifacts.keys():
                ga = ma.getGA()
                if not ga in gas:
                    gas.append(ga)
            try:
                if self.configuration.useCache:
                    path_dict = carto.paths(_wsid, sourceKey, gavs, gas, excludedSources, excludedSubgraphs, preset,
                                            mutator, patcherIds, injectedBOMs, False)
                else:
                    path_dict = carto.paths_nocache(_wsid, sourceKey, gavs, gas, excludedSources, excludedSubgraphs,
                                                    preset, mutator, patcherIds, injectedBOMs, False)
            except RuntimeError:
                path_dict = dict()
            if "projects" in path_dict:
                path_dict = path_dict["projects"]
            if path_dict:
                for ma in artifacts.keys():
                    for key in path_dict.keys():
                        key_gav = key
                        if key.count(":") > 2:
                            key_split = key.split(":")
                            key_gav = ":".join([key_split[0], key_split[1], key_split[-1]])
                        if ma.getGAV() == key_gav:
                            gav_paths = path_dict[key]
                            if type(gav_paths) is dict:
                                gav_paths = gav_paths["paths"]
                            for gav_path in gav_paths:
                                direct = True
                                if type(gav_path) is dict:
                                    gav_path = gav_path["pathParts"]
                                rel_path = []
                                for gav_rel in gav_path:
                                    if "inherited" in gav_rel and gav_rel["inherited"]:
                                        direct = False
                                        break
                                    declaring = MavenArtifact.createFromGAV(gav_rel["declaring"])
                                    target = MavenArtifact.createFromGAV(maven_repo_util.gatvc_to_gatcv(gav_rel["target"]))
                                    rel_type = gav_rel["type"] if "type" in gav_rel else gav_rel["rel"]
                                    if rel_type == "DEPENDENCY":
                                        extra = gav_rel["scope"]
                                        if "optional" in gav_rel and gav_rel["optional"]:
                                            extra = "%s %s" % (extra, "optional")
                                        rel = ArtifactRelationship(declaring, target, rel_type, extra)
                                    elif rel_type == "PLUGIN_DEP":
                                        rel = ArtifactRelationship(declaring, target, rel_type, gav_rel["plugin"])
                                    elif rel_type == "BOM":
                                        if "mixin" in gav_rel and gav_rel["mixin"]:
                                            direct = False
                                            break
                                        rel = ArtifactRelationship(declaring, target, rel_type)
                                    else:
                                        rel = ArtifactRelationship(declaring, target, rel_type)
                                    rel_path.append(rel)

                                if direct:
                                    artifacts[ma].add_path(rel_path)
            for ma in artifacts.keys():
                if not artifacts[ma].paths and ma.getGAV() not in gavs:
                    # create artificial unknown paths from all roots to current artifact
                    for root in gavs:
                        if root != ma.getGAV():
                            rel_path = [ArtifactRelationship(MavenArtifact.createFromGAV(root), None, None),
                                        ArtifactRelationship(None, ma, None)]
                            artifacts[ma].add_path(rel_path)

            if not wsid:
                try:
                    carto.deleteWorkspace(_wsid)
                except Exception as err:
                    logging.warning("Workspace deletion failed: %s" % str(err))

        return artifacts

    def _listRepository(self, repoUrls, gavPatterns, gatcvs):
        """
        Loads maven artifacts from a repository.

        :param repoUrl: repository URL (local or remote, supported are [file://], http:// and
                        https:// urls)
        :param gavPatterns: list of patterns to filter by GAV
        :returns: Dictionary where index is MavenArtifact object and value is ArtifactSpec with its
                  repo root URL.
        """

        if gatcvs:
            prefixes = self._getPrefixesGatcvs(gatcvs)
            classifiersFilter = self._getClassifiersFilter(gatcvs)
        else:
            prefixes = self._getPrefixes(gavPatterns)
            classifiersFilter = {}
        artifacts = {}
        for repoUrl in reversed(repoUrls):
            urlWithSlash = maven_repo_util.slashAtTheEnd(repoUrl)
            protocol = maven_repo_util.urlProtocol(urlWithSlash)
            if protocol == 'file':
                for prefix in prefixes:
                    artifacts.update(self._listLocalRepository(urlWithSlash[7:], prefix))
            elif protocol == '':
                for prefix in prefixes:
                    artifacts.update(self._listLocalRepository(urlWithSlash, prefix))
            elif protocol == 'http' or protocol == 'https':
                for prefix in prefixes:
                    artifacts.update(self._listRemoteRepository(urlWithSlash, classifiersFilter, prefix))
            elif protocol == 'indy' or protocol == 'indys':
                for prefix in prefixes:
                    artifacts.update(self._listIndyRepository(urlWithSlash, classifiersFilter, prefix))
            else:
                raise "Invalid protocol!", protocol

        if gatcvs:
            artifacts = self._filterArtifactsByPatterns(artifacts, None, gatcvs)
        else:
            artifacts = self._filterArtifactsByPatterns(artifacts, gavPatterns, None)
        logging.debug("Found %d artifacts", len(artifacts))

        return artifacts

    def _getPrefixesGatcvs(self, gatcvsList):
        # Match pattern ((?:groupId:)(?:artifactId:))(?:type:)?(?:classifier:)?(version)(?::scope)?
        _regexGATCVS = re.compile('((?:[\w\-.]+:){2})(?:[\w\-.]+:){0,2}([\d][\w\-.]+)(?::(?:compile|provided|runtime|test'
                                  '|system|import))?')
        gavList = []
        for gatcvs in gatcvsList:
            match = _regexGATCVS.search(gatcvs)
            if match:
                gavList.append("%s%s" % (match.group(1), match.group(2)))
        return self._getPrefixes(gavList)

    def _getClassifiersFilter(self, gatcvsList):
        # Match pattern (groupId):(artifactId):(type):(classifier):(version)(?::scope)?
        _regexGATCVS = re.compile('([\w\-.]+):([\w\-.]+):([\w\-.]+):([\w\-.]+):([\d][\w\-.]+)'
                                  '(?::(?:compile|provided|runtime|test|system|import))?')
        classifiersFilter = {}
        for gatcvs in gatcvsList:
            match = _regexGATCVS.search(gatcvs)
            if match:
                gav = match.group(1, 2, 5)
                classifiersFilter.setdefault(gav, {}).setdefault(match.group(3), set()).add(match.group(4))
        return classifiersFilter

    def _getPrefixes(self, gavPatterns):
        if not gavPatterns:
            return set([''])
        repat = re.compile("^r/.*/$")
        prefixrepat = re.compile("^(([a-zA-Z0-9-]+|\\\.|:)+)")
        patterns = set()
        for pattern in gavPatterns:
            if repat.match(pattern):  # if pattern is regular expression pattern "r/expr/"
                kp = prefixrepat.match(pattern[2:-1])
                if kp:
                    # if the expr starts with readable part (eg. "r/org\.jboss:core-.*:.*/")
                    # convert readable part to asterisk string: "org.jboss:*"
                    pattern = kp.group(1).replace("\\", "") + "*"
                else:
                    return set([''])
            p = pattern.split(":")
            px = p[0].replace(".", "/") + "/"  # GroupId
            if len(p) >= 2:
                px += p[1] + "/"               # ArtifactId
            if len(p) >= 3:
                px += p[2] + "/"               # Version
            pos = px.find("*")
            if pos != -1:
                px = px[:pos]
            partitions = px.rpartition("/")
            if partitions[0]:
                patterns.add(partitions[0] + "/")
            else:
                # in case there is no slash before the first star
                return set([''])

        prefixes = set()
        while patterns:
            pattern = patterns.pop()
            for prefix in patterns | prefixes:
                if pattern.startswith(prefix):
                    break
            else:
                prefixes.add(pattern)
        return prefixes

    def _listRemoteRepository(self, repoUrl, classifiersFilter, prefix=""):
        logging.debug("Listing remote repository %s prefix '%s'", repoUrl, prefix)
        try:
            out = self._lftpFind(repoUrl + prefix)
        except IOError as err:
            if prefix:
                logging.warning(str(err))
                out = ""
            else:
                raise err

        # ^./(groupId)/(artifactId)/(version)/(filename)$
        regexGAVF = re.compile(r'\./(.+)/([^/]+)/([^/]+)/([^/]+\.[^/.]+)$')
        gavExtClass = {}  # { (g,a,v): {ext: set([class])} }
        suffixes = {}     # { (g,a,v): suffix }
        for line in out.split('\n'):
            if (line):
                line = "./" + prefix + line[2:]
                gavf = regexGAVF.match(line)
                if gavf is not None:
                    groupId = gavf.group(1).replace('/', '.')
                    artifactId = gavf.group(2)
                    version = gavf.group(3)
                    filename = gavf.group(4)

                    if filename in self.IGNORED_REPOSITORY_FILES:
                        continue

                    (extsAndClass, suffix) = self._getExtensionsAndClassifiers(artifactId, version, [filename])

                    gav = (groupId, artifactId, version)

                    gavExtClass.setdefault(gav, {})
                    self._updateExtensionsAndClassifiers(gavExtClass[gav], extsAndClass, classifiersFilter.get(gav))

                    if suffix is not None and (gav not in suffixes or suffixes[gav] < suffix):
                        suffixes[gav] = suffix

        artifacts = {}
        for gav in gavExtClass:
            self._addArtifact(artifacts, gav[0], gav[1], gav[2], gavExtClass[gav], suffixes.get(gav), repoUrl)
        return artifacts

    def _listIndyRepository(self, repoUrl, classifiersFilter, prefix=""):
        logging.debug("Listing Indy remote repository %s prefix '%s'", repoUrl, prefix)
        url = repoUrl.replace('indy://', 'http://').replace('indys://', 'https://') + prefix

        try:
            response = requests.get(url)
            if response.status_code != 200:
                logging.warning("Cannot retrieve listing for: " + url + ". Error: " + str(response))
                return {}
            else:
                out = response.json()
                # logging.debug(out)
        except IOError as err:
            if prefix:
                logging.warning(str(err))
                out = {}
            else:
                raise err

        # ^/(groupId)/(artifactId)/(version)/(filename)$
        regexGAVF = re.compile(r'/(.+)/([^/]+)/([^/]+)/([^/]+\.[^/.]+)$')
        gavExtClass = {}  # { (g,a,v): {ext: set([class])} }
        suffixes = {}     # { (g,a,v): suffix }

        listings = out.get('listingUrls')
        # logging.debug(listings)
        if listings is None or len(listings) < 1:
            logging.warning("No results from: " + url)
            return {}

        for listing in listings:
            line = listing['path']
            # logging.debug("Process listing: " + line)
            if (line):
                gavf = regexGAVF.match(line)
                # if gavf is None:
                #     logging.debug("No GAVTC match for: " + line)
                # else:
                if gavf is not None:
                    # logging.debug("Group 1 of regex: " + gavf.group(1))
                    groupId = gavf.group(1).replace('/', '.')
                    artifactId = gavf.group(2)
                    version = gavf.group(3)
                    filename = gavf.group(4)

                    # logging.debug("Found G: %s, A: %s, V: %s, F: %s" % (groupId, artifactId, version, filename))

                    if filename in self.IGNORED_REPOSITORY_FILES:
                        # logging.debug( "Ignoring: " + filename )
                        continue

                    (extsAndClass, suffix) = self._getExtensionsAndClassifiers(artifactId, version, [filename])

                    gav = (groupId, artifactId, version)

                    gavExtClass.setdefault(gav, {})
                    self._updateExtensionsAndClassifiers(gavExtClass[gav], extsAndClass, classifiersFilter.get(gav))

                    if suffix is not None and (gav not in suffixes or suffixes[gav] < suffix):
                        suffixes[gav] = suffix

        artifacts = {}
        for gav in gavExtClass:
            self._addArtifact(artifacts, gav[0], gav[1], gav[2], gavExtClass[gav], suffixes.get(gav), repoUrl)
        return artifacts

    def _listLocalRepository(self, directoryPath, prefix=""):
        """
        Loads maven artifacts from local directory.

        :param directoryPath: Path of the local directory.
        :returns: Dictionary where index is MavenArtifact object and value is ArtifactSpec with its
                  repo root URL starting with 'file://'.
        """
        logging.debug("Listing local repository %s prefix '%s'", directoryPath, prefix)
        artifacts = {}
        # ^(groupId)/(artifactId)/(version)/?$
        regexGAV = re.compile(r'^(.+)/([^/]+)/([^/]+)/?$')
        for dirname, dirnames, filenames in os.walk(directoryPath + prefix, followlinks=True):
            if filenames:
                logging.debug("Looking for artifacts in %s", dirname)
                gavPath = dirname.replace(directoryPath, '')
                gav = regexGAV.search(gavPath)
                #If gavPath is e.g. example/sth, then gav is None
                if not gav:
                    continue

                # Remove first slash if present then convert to GroupId
                groupId = re.sub("^/", "", gav.group(1)).replace('/', '.')
                artifactId = gav.group(2)
                version = gav.group(3)

                filteredFilenames = list(set(filenames) - self.IGNORED_REPOSITORY_FILES)
                if filteredFilenames:
                    (extsAndClass, suffix) = self._getExtensionsAndClassifiers(artifactId, version, filteredFilenames)

                    url = "file://" + directoryPath
                    self._addArtifact(artifacts, groupId, artifactId, version, extsAndClass, suffix, url)

        return artifacts

    def _getExtensionsAndClassifiers(self, artifactId, version, filenames):
        # returns ({ext: set([classifier])}, suffix)
        av = self._getArtifactVersionREString(artifactId, version)
        # artifactId-(version)-(classifier).(extension)
        #                          (classifier)   (   extension   )
        checksumRegEx = re.compile(av + ".+\.(md5|sha1|sha256|asc)$")
        ceRegEx1 = re.compile(av + "(?:-(.+))?\.(tar\.[^.]+)$")
        ceRegEx2 = re.compile(av + "(?:-(.+))?\.([^.]+)$")

        suffix = None
        extensions = {}
        for filename in filenames:
            cs = checksumRegEx.match(filename)
            if cs:
                # the file is a checksum, not an artifact
                continue

            ce = ceRegEx1.match(filename)
            if not ce:
                ce = ceRegEx2.match(filename)
            if ce:
                realVersion = ce.group(1)
                classifier = ce.group(2)
                ext = ce.group(3)

                extensions.setdefault(ext, set())
                if classifier is None:
                    extensions[ext].add("")
                else:
                    extensions[ext].add(classifier)

                if realVersion != version:
                    if suffix is None or suffix < realVersion:
                        suffix = realVersion
        return (extensions, suffix)

    def _addArtifact(self, artifacts, groupId, artifactId, version, extsAndClass, suffix, url):
        pomMain = True
        # The pom is main only if no other main artifact is available
        if len(extsAndClass) > 1 and self._containsMainArtifact(extsAndClass) and "pom" in extsAndClass:
            pomMain = False

        artTypes = []
        for ext, classifiers in extsAndClass.iteritems():
            main = ext == "pom" and pomMain
            if not main:
                for classifier in classifiers:
                    extClassifier = "%s:%s" % (ext, classifier or "")
                    main = extClassifier not in self.notMainExtClassifiers
                    if main:
                        break
            artTypes.append(ArtifactType(ext, main, classifiers))

        mavenArtifact = MavenArtifact(groupId, artifactId, None, version)
        if suffix is not None:
            mavenArtifact.snapshotVersionSuffix = suffix
        if mavenArtifact in artifacts:
            artifacts[mavenArtifact].merge(ArtifactSpec(url, artTypes))
        else:
            artifacts[mavenArtifact] = ArtifactSpec(url, artTypes)

    def _containsMainArtifact(self, extsAndClass):
        """
        Checks if the given dictionary with structure extension -> classifier[] contains a combination
        of extension and classifier other than those included in notMainExtClassifiers.

        :param extsAndClass: the dictionary
        :returns: True if such a combination is found, False otherwise
        """
        result = False
        for ext in extsAndClass:
            for classifier in extsAndClass[ext]:
                extClassifier = "%s:%s" % (ext, classifier or "")
                if extClassifier not in self.notMainExtClassifiers:
                    result = True
                    break
        return result

    def _updateExtensionsAndClassifiers(self, d, u, classifiersFilter=None):
        allClassifiers = self.configuration.isAllClassifiers()
        for extension, classifiers in u.iteritems():
            if allClassifiers:
                d.setdefault(extension, set()).update(classifiers)
            else:
                for classifier in classifiers:
                    if not classifier:
                        d.setdefault(extension, set()).add(classifier)
                    else:
                        for extClass in self.configuration.addClassifiers:
                            addExtension = extClass["type"]
                            addClass = extClass["classifier"]
                            if extension == addExtension and classifier == addClass:
                                d.setdefault(extension, set()).add(classifier)
                                break
                        else:
                            if classifiersFilter:
                                broken = False
                                for addExtension in classifiersFilter.keys():
                                    for addClass in classifiersFilter[addExtension]:
                                        if extension == addExtension and classifier == addClass:
                                            d.setdefault(extension, set()).add(classifier)
                                            broken = True
                                            break
                                    if broken:
                                        break

    def _getArtifactVersionREString(self, artifactId, version):
        if version.endswith("-SNAPSHOT"):
            # """Prepares the version string to be part of regular expression for filename and when the
            # version is a snapshot version, it corrects the suffix to match even when the files are
            # named with the timestamp and build number as usual in case of snapshot versions."""
            versionPattern = version.replace("SNAPSHOT", r'(SNAPSHOT|\d+\.\d+-\d+)')
        else:
            versionPattern = "(" + re.escape(version) + ")"
        return re.escape(artifactId) + "-" + versionPattern

    def _listArtifacts(self, urls, gavs):
        """
        Loads maven artifacts from list of GAVs and tries to locate the artifacts in one of the
        specified repositories.

        :param urls: repository URLs where the given GAVs can be located
        :param gavs: List of GAVs
        :returns: Dictionary where index is MavenArtifact object and value is it's repo root URL.
        """
        def findArtifact(gav, urls, artifacts):
            artifact = MavenArtifact.createFromGAV(gav)
            for url in urls:
                if maven_repo_util.gavExists(url, artifact):
                    #Critical section?
                    artifacts[artifact] = ArtifactSpec(url, [ArtifactType(artifact.artifactType, True, set(['']))])
                    return

            logging.warning('Artifact %s not found in any url!', artifact)

        artifacts = {}
        pool = ThreadPool(maven_repo_util.MAX_THREADS)
        for gav in gavs:
            pool.apply_async(findArtifact, [gav, urls, artifacts])

        # Close the pool and wait for the workers to finnish
        pool.close()
        pool.join()

        return artifacts

    def _parseDepList(self, depList):
        """Parse maven dependency:list output and return a list of GAVs"""
        regexComment = re.compile('#.*$')
        # Match pattern groupId:artifactId:[type:][classifier:]version[:scope]
        regexGAV = re.compile('(([\w\-.]+:){2,3}([\w\-.]+:)?([\d][\w\-.]*))(:[\w]*\S)?')
        gavList = []
        for nextLine in depList:
            nextLine = regexComment.sub('', nextLine)
            nextLine = nextLine.strip()
            gav = regexGAV.search(nextLine)
            if gav:
                gavList.append(gav.group(1))

        return gavList

    def _filterArtifactsByPatterns(self, artifacts, gavPatterns, gatcvs):
        if not gavPatterns and not gatcvs:
            return artifacts

        includedArtifacts = {}
        if gatcvs:
            for artifact in artifacts.keys():
                artSpec = artifacts[artifact]
                artTypes = {}
                extContainsMain = False
                for ext in artSpec.artTypes.keys():
                    if ext == "pom":
                        main = len(artSpec.artTypes.keys()) == 1
                        if not main:
                            gatcv = "%s:%s:%s" % (artifact.getGA(), ext, artifact.version)
                            if gatcv in gatcvs:
                                main = True
                        extContainsMain = extContainsMain or main

                        pomType = ArtifactType(ext, main, set(['']))
                        artTypes[ext] = pomType
                    else:
                        main = False
                        classifiers = set()
                        for classifier in artSpec.artTypes[ext].classifiers:
                            if classifier:
                                gatcv = "%s:%s:%s:%s" % (artifact.getGA(), ext, classifier, artifact.version)
                            else:
                                gatcv = "%s:%s:%s" % (artifact.getGA(), ext, artifact.version)

                            if gatcv in gatcvs:
                                classifiers.add(classifier)
                                main = True
                            else:
                                if self._containedInAddClassifiers(ext, classifier):
                                    classifiers.add(classifier)

                        extContainsMain = extContainsMain or main

                        artType = ArtifactType(ext, main, classifiers)
                        artTypes[ext] = artType
                if extContainsMain:
                    artSpecToAdd = ArtifactSpec(artSpec.url, artTypes)
                    includedArtifacts[artifact] = artSpecToAdd
        else:
            regExps = maven_repo_util.getRegExpsFromStrings(gavPatterns)
            for artifact in artifacts.keys():
                if maven_repo_util.somethingMatch(regExps, artifact.getGAV()):
                    includedArtifacts[artifact] = artifacts[artifact]
        return includedArtifacts

    def _containedInAddClassifiers(self, extension, classifier):
        result = False

        if self.configuration.isAllClassifiers():
            result = True
        else:
            for extClass in self.configuration.addClassifiers:
                addExtension = extClass["type"]
                addClass = extClass["classifier"]
                if extension == addExtension and classifier == addClass:
                    result = True
                    break

        return result

    def _lftpFind(self, url):
        if maven_repo_util.urlExists(url):
            lftp = Popen(r'lftp -c "set ssl:verify-certificate no ; open ' + url
                         + ' && find  ."', stdout=PIPE, shell=True)
            result = lftp.communicate()[0]
            if lftp.returncode:
                raise IOError("lftp find in %s ended by return code %d" % (url, lftp.returncode))
            else:
                return result
        else:
            raise IOError("Cannot list URL %s. The URL does not exist." % url)


class ArtifactSpec():
    """
    Specification of artifact location and contents. The artTypes is a dictionary with type as a key and an
    ArtifactType instance as a value. It is automatically created if the provided value is a list.
    """

    def __init__(self, url, artTypes):
        """
        Constructor.

        :param url: repository URL in which the artifact was found
        :param artTypes: dict or list of ArtifactType instances
        """
        self.url = url
        if type(artTypes) is dict:
            self.artTypes = artTypes
        else:
            self.artTypes = {}
            for artType in artTypes:
                self.artTypes[artType.artType] = artType
        self.paths = []

    def merge(self, other):
        if other.url and self.url != other.url:
            raise ValueError("Cannot merge artifact specs with different URLs (%s != %s)." % (self.url, other.url))

        for artType in other.artTypes.keys():
            if artType in self.artTypes:
                raise ValueError("Cannot merge artifact specs with overlapping types (%s vs %s)."
                                 % (str(self.artTypes.keys()), str(other.artTypes.keys())))

        self.artTypes.update(other.artTypes)
        self.paths.extend(other.paths)

    def add_path(self, path):
        """
        Adds a relationship path into paths set.

        :param path: a list of artifact relationships from root to the current artifact
        """
        self.paths.append(path)

    def containsMain(self):
        """
        Checks if there is a main artifact type in this instance.

        :returns: True if a main type exists, False otherwise
        """
        for artType in self.artTypes.keys():
            if self.artTypes[artType].mainType:
                return True
        return False

    def __str__(self):
        return "%s %s" % (self.url, str(self.artTypes))

    def __repr__(self):
        return "ArtifactSpec(%s, %s)" % (repr(self.url), repr(self.artTypes))


class ArtifactRelationship():
    """
    Part of relationship path between root artifact and an artifact contained in repository. Each relationship has its
    type in field rel, declaring artifact and target artifact. When the relationship is of type DEPENDENCY, then also
    scope is stored.
    """

    def __init__(self, declaring, target, rel_type, extra=None):
        """
        :param declaring: the declaring artifact
        :param target: the target artifact
        :param rel_type: the relationship type, available values are "PARENT", "DEPENDENCY", "BOM"
        :param extra: extra info for different relationship types, i.e. scope for dependencies, plugin for plugin deps
        """
        self.declaring = declaring
        self.target = target
        self.rel_type = rel_type
        self.extra = extra

    def __cmp__(self, other):
        result = cmp(self.declaring, other.declaring)
        if result == 0:
            result = cmp(self.rel_type, other.rel_type)
        if result == 0:
            result = cmp(self.extra, other.extra)
        if result == 0:
            result = cmp(self.target, other.target)
        return result


class ArtifactType():
    """
    Artifact type with classifiers and information, if it is considered as a main type. A type is considered main when
    it is different from pom and has an empty classifier or it is requested by user in GATCV filter. I.e. when such
    artifacts exist:
        artifact-1.0.pom
        artifact-1.0-sources.jar
        artifact-1.0.war
    the "war" type is considered as main. If there is also artifact-1.0.jar file, there are 2 main types "jar" and
    "war". Also if there is a group:artifact:jar:sources:1.0 filter item the "jar" is considered main too. If there is
    nothing else than pom, then it is considered the main type.
    """

    def __init__(self, artType, mainType, classifiers):
        self.artType = artType
        self.mainType = mainType
        self.classifiers = classifiers

    def __str__(self):
        if self.mainType:
            main = " (main)"
        else:
            main = ""
        return "%s%s: %s" % (self.artType, main, str(self.classifiers))

    def __repr__(self):
        return "ArtifactType(%s, %s, %s)" % (repr(self.artType), repr(self.mainType), repr(self.classifiers))
