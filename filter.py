import copy
import logging
from multiprocessing.pool import ThreadPool

import maven_repo_util
from maven_artifact import MavenArtifact


class Filter:

    def __init__(self, config):
        self.config = config

    def filter(self, artifactList, threadnum):
        """
        Filter artifactList removing excluded GAVs, duplicates and GAVs that exists in
        excluded repositories.

        :param artifactList: artifactList from ArtifactListBuilder.
        :returns: filtered artifactList.
        """

        if self.config.excludedGAVs:
            artifactList = self._filterExcludedGAVs(artifactList)

        if self.config.excludedTypes:
            artifactList = self._filterExcludedTypes(artifactList)

        artifactList = self._filterDuplicates(artifactList)

        if self.config.singleVersion:
            artifactList = self._filterMultipleVersions(artifactList)

        if self.config.excludedRepositories:
            artifactList = self._filterExcludedRepositories(artifactList, threadnum)

        return artifactList

    def _filterExcludedGAVs(self, artifactList):
        """
        Filter artifactList removing specified GAVs.

        :param artifactList: artifactList to be filtered.
        :returns: artifactList without artifacts that matched specified GAVs.
        """

        logging.debug("Filtering artifacts with excluded GAVs.")
        regExps = maven_repo_util.getRegExpsFromStrings(self.config.excludedGAVs)
        gavRegExps = []
        gatcvRegExps = []
        for regExp in regExps:
            if regExp.pattern.count(":") > 2:
                gatcvRegExps.append(regExp)
            else:
                gavRegExps.append(regExp)
        for ga in artifactList.keys():
            for priority in artifactList[ga].keys():
                for version in artifactList[ga][priority].keys():
                    gav = "%s:%s" % (ga, version)
                    if maven_repo_util.somethingMatch(gavRegExps, gav):
                        logging.debug("Dropping GAV %s:%s from priority %i because it matches an excluded "
                                      "GAV pattern.", ga, version, priority)
                        del artifactList[ga][priority][version]
                    else:
                        artSpec = artifactList[ga][priority][version]
                        for artType in copy.deepcopy(artSpec.artTypes.keys()):
                            at = artSpec.artTypes[artType]
                            for classifier in copy.deepcopy(at.classifiers):
                                if classifier:
                                    gatcv = "%s:%s:%s:%s" % (ga, artType, classifier, version)
                                else:
                                    gatcv = "%s:%s:%s" % (ga, artType, version)
                                if maven_repo_util.somethingMatch(gatcvRegExps, gatcv):
                                    logging.debug("Dropping GATCV %s from priority %i because it matches an excluded "
                                                  "GAV pattern.", gatcv, priority)
                                    at.classifiers.remove(classifier)
                            if not at.classifiers:
                                logging.debug("Dropping GATV %s:%s:%s from priority %i because of no classifiers left.",
                                              ga, artType, version, priority)
                                del artSpec.artTypes[artType]
                        if not artSpec.containsMain():
                            logging.debug("Dropping GAV %s:%s from priority %i because of no main artifact left.",
                                          ga, version, priority)
                            del artifactList[ga][priority][version]
                if not artifactList[ga][priority]:
                    logging.debug("Dropping GA %s from priority %i because of no version left.", ga, priority)
                    del artifactList[ga][priority]
            if not artifactList[ga]:
                logging.debug("Dropping GA %s because of no priority left.", ga)
                del artifactList[ga]
        return artifactList

    def _filterExcludedTypes(self, artifactList):
        '''
        Filter artifactList removing GAVs with specified main types only, otherwise keeping GAVs with
        not-excluded artifact types only.

        :param artifactList: artifactList to be filtered.
        :param exclTypes: list of excluded types
        :returns: artifactList without artifacts that matched specified types and had no other main types.
        '''
        logging.debug("Filtering artifacts with excluded types.")
        regExps = maven_repo_util.getRegExpsFromStrings(self.config.gatcvWhitelist)
        exclTypes = self.config.excludedTypes
        for ga in artifactList.keys():
            for priority in artifactList[ga].keys():
                for version in artifactList[ga][priority].keys():
                    artSpec = artifactList[ga][priority][version]
                    for artType in list(artSpec.artTypes.keys()):
                        if artType in exclTypes:
                            artTypeObj = artSpec.artTypes[artType]
                            classifiers = artTypeObj.classifiers
                            (groupId, artifactId) = ga.split(':')
                            for classifier in list(classifiers):
                                art = MavenArtifact(groupId, artifactId, artType, version, classifier)
                                gatcv = art.getGATCV()
                                if not maven_repo_util.somethingMatch(regExps, gatcv):
                                    logging.debug("Dropping classifier \"%s\" of %s:%s:%s from priority %i because of "
                                                  "excluded type.", classifier, ga, artType, version, priority)
                                    classifiers.remove(classifier)
                                else:
                                    logging.debug("Skipping drop of %s:%s:%s:%s from priority %i because it matches a "
                                                  "whitelist pattern.", ga, artType, classifier, version, priority)
                            if not classifiers:
                                logging.debug("Dropping %s:%s:%s from priority %i because of no classifier left.", ga,
                                              artType, version, priority)
                                del(artSpec.artTypes[artType])
                    noMain = True
                    for artType in artSpec.artTypes.keys():
                        artTypeObj = artSpec.artTypes[artType]
                        if artTypeObj.mainType:
                            noMain = False
                            break
                    if not artSpec.artTypes or noMain:
                        if noMain:
                            logging.debug("Dropping GAV %s:%s from priority %i because of no main artifact left.",
                                          ga, version, priority)
                        else:
                            logging.debug("Dropping GAV %s:%s from priority %i because of no artifact type left.",
                                          ga, version, priority)
                        del artifactList[ga][priority][version]
                if not artifactList[ga][priority]:
                    logging.debug("Dropping GA %s from priority %i because of no version left.", ga, priority)
                    del artifactList[ga][priority]
            if not artifactList[ga]:
                logging.debug("Dropping GA %s because of no priority left.", ga)
                del artifactList[ga]
        return artifactList

    def _filterExcludedRepositories(self, artifactList, threadnum):
        """
        Filter artifactList removing artifacts existing in specified repositories.

        :param artifactList: artifactList to be filtered.
        :returns: artifactList without artifacts that exists in specified repositories.
        """

        logging.debug("Filtering artifacts contained in excluded repositories.")

        pool = ThreadPool(threadnum)
        # Contains artifact to be removed
        delArtifacts = []
        for ga in artifactList.keys():
            groupId = ga.split(':')[0]
            artifactId = ga.split(':')[1]
            for priority in artifactList[ga].keys():
                for version in artifactList[ga][priority].keys():
                    artifact = MavenArtifact(groupId, artifactId, "pom", version)
                    pool.apply_async(
                        _artifactInRepos,
                        [self.config.excludedRepositories, artifact, priority, delArtifacts]
                    )

        # Close the pool and wait for the workers to finnish
        pool.close()
        pool.join()
        for artifact, priority in delArtifacts:
            ga = artifact.getGA()
            logging.debug("Dropping GAV %s:%s from priority %i because it was found in an excluded repository.",
                          ga, artifact.version, priority)
            del artifactList[ga][priority][artifact.version]
            if not artifactList[ga][priority]:
                logging.debug("Dropping GA %s from priority %i because of no version left.", ga, priority)
                del artifactList[ga][priority]
            if not artifactList[ga]:
                logging.debug("Dropping GA %s because of no priority left.", ga)
                del artifactList[ga]

        return artifactList

    def _filterDuplicates(self, artifactList):
        """
        Filter artifactList removing duplicate artifacts.

        :param artifactList: artifactList to be filtered.
        :returns: artifactList without duplicate artifacts from lower priorities.
        """

        logging.debug("Filtering duplicate artifacts.")
        for ga in artifactList.keys():
            for priority in sorted(artifactList[ga].keys()):
                for version in artifactList[ga][priority].keys():
                    for pr in artifactList[ga].keys():
                        if pr <= priority:
                            continue
                        if version in artifactList[ga][pr]:
                            logging.debug("Dropping GAV %s:%s from priority %i because its duplicate was found in "
                                          "priority %s.", ga, version, pr, priority)
                            if len(artifactList[ga][pr][version].paths):
                                artifactList[ga][priority][version].paths.extend(artifactList[ga][pr][version].paths)
                            del artifactList[ga][pr][version]
                if not artifactList[ga][priority]:
                    logging.debug("Dropping GA %s from priority %i because of no version left.", ga, priority)
                    del artifactList[ga][priority]
            if not artifactList[ga]:
                logging.debug("Dropping GA %s because of no priority left.", ga)
                del artifactList[ga]
        return artifactList

    def _filterMultipleVersions(self, artifactList):
        logging.debug("Filtering multi-version artifacts to have just a single version.")
        regExps = maven_repo_util.getRegExpsFromStrings(self.config.multiVersionGAs, False)

        for ga in sorted(artifactList.keys()):
            if maven_repo_util.somethingMatch(regExps, ga):
                continue

            # Gather all priorities
            priorities = sorted(artifactList[ga].keys())
            priority = priorities[0]
            # Gather all versions
            versions = list(artifactList[ga][priority].keys())

            if len(versions) > 1:  # list of 1 is sorted by definition
                versions = maven_repo_util._sortVersionsWithAtlas(versions)

            # Remove version, priorities and gats from artifactList as necessary
            for version in versions[1:]:
                logging.debug("Dropping GAV %s:%s from priority %i because only single version is allowed.", ga,
                              version, priority)
                del artifactList[ga][priority][version]
            for p in priorities[1:]:
                logging.debug("Dropping GA %s from priority %i because of no version left.", ga, p)
                del artifactList[ga][p]
            if not artifactList[ga]:
                logging.debug("Dropping GA %s because of no priority left.", ga)
                del artifactList[ga]

        return artifactList


def _artifactInRepos(repositories, artifact, priority, artifacts):
    """
    Checks if artifact is available in one of the repositories, if so, appends
    it with priority in list of pairs - artifacts. Used for multi-threading.

    :param repositories: list of repository urls
    :param artifact: searched MavenArtifact
    :param priority: value of dictionary artifacts
    :param artifacts: list with (artifact, priority) tuples
    """
    for repoUrl in repositories:
        if maven_repo_util.gavExists(repoUrl, artifact):
            #Critical section?
            artifacts.append((artifact, priority))
            break
