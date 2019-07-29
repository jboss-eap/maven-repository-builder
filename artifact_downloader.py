"""
artifact_downloader.py: Fetch artifacts into a location, where a Maven repository is being built given
a list of artifacts and a remote repository URL.
"""

import logging
import os
import re
import urlparse
from multiprocessing import Queue
from multiprocessing import Lock
from multiprocessing.pool import ThreadPool

import maven_repo_util
from maven_artifact import MavenArtifact


def downloadArtifacts(remoteRepoUrl, localRepoDir, artifact, checksumMode, mkdirLock, filesetLock, fileset, errors):
    """Download artifact from a remote repository."""
    logging.debug("Starting download of %s", str(artifact))

    artifactLocalDir = os.path.join(localRepoDir, artifact.getDirPath())

    try:
        # handle parallelism, when two threads checks if a directory exists and then both tries to create it
        mkdirLock.acquire()
        if not os.path.exists(artifactLocalDir):
            os.makedirs(artifactLocalDir)
        mkdirLock.release()

        remoteRepoUrl = maven_repo_util.slashAtTheEnd(remoteRepoUrl)

        # Download main artifact
        artifactUrl = remoteRepoUrl + artifact.getArtifactFilepath()
        artifactLocalPath = os.path.join(localRepoDir, artifact.getArtifactFilepath())
        maven_repo_util.fetchFile(artifactUrl, artifactLocalPath, checksumMode, True, True, filesetLock, fileset)
    except BaseException as ex:
        logging.error("Error while downloading artifact %s: %s", artifact, str(ex))
        errors.put(ex)


def copyArtifact(remoteRepoPath, localRepoDir, artifact, checksumMode):
    """Copy artifact from a repository on the local file system along with pom and source jar"""
    # Copy main artifact
    artifactPath = os.path.join(remoteRepoPath, artifact.getArtifactFilepath())
    artifactLocalPath = os.path.join(localRepoDir, artifact.getArtifactFilepath())
    if os.path.exists(artifactPath) and not os.path.exists(artifactLocalPath):
        maven_repo_util.fetchFile(artifactPath, artifactLocalPath, checksumMode)


def depListToArtifactList(depList):
    """Convert the maven GAV to a URL relative path"""
    regexComment = re.compile('#.*$')
    #regexLog = re.compile('^\[\w*\]')
    artifactList = []
    for nextLine in depList:
        nextLine = regexComment.sub('', nextLine)
        nextLine = nextLine.strip()
        gav = maven_repo_util.parseGATCVS(nextLine)
        if gav:
            artifactList.append(MavenArtifact.createFromGAV(gav))
    return artifactList


def fetchArtifactList(remoteRepoUrl, localRepoDir, artifactList, checksumMode, threadnum):
    remoteRepoUrl = remoteRepoUrl.replace('indy://', 'http://').replace('indys://', 'https://')
    """Create a Maven repository based on a remote repository url and a list of artifacts"""
    logging.info('Retrieving artifacts from repository: %s', remoteRepoUrl)
    if not os.path.exists(localRepoDir):
        os.makedirs(localRepoDir)
    parsedUrl = urlparse.urlparse(remoteRepoUrl)
    protocol = parsedUrl[0]
    repoPath = parsedUrl[2]

    if protocol == 'http' or protocol == 'https':
        # Create thread pool
        pool = ThreadPool(threadnum)
        errors = Queue()
        mkdirLock = Lock()
        filesetLock = Lock()
        fileset = set([])

        for artifact in artifactList:
            if artifact.isSnapshot():
                maven_repo_util.updateSnapshotVersionSuffix(artifact, remoteRepoUrl)
            pool.apply_async(
                downloadArtifacts,
                [remoteRepoUrl, localRepoDir, artifact, checksumMode, mkdirLock, filesetLock, fileset, errors]
            )

        # Close pool and wait till all workers are finished
        pool.close()
        pool.join()

        # If one of the workers threw an error, log it
        if not errors.empty():
            logging.error("During fetching files from repository %s %i error(s) occurred.", remoteRepoUrl,
                          errors.qsize())

    elif protocol == 'file':
        repoPath = remoteRepoUrl.replace('file://', '')
        for artifact in artifactList:
            if artifact.isSnapshot():
                maven_repo_util.updateSnapshotVersionSuffix(artifact, remoteRepoUrl)
            copyArtifact(repoPath, localRepoDir, artifact, checksumMode)
    else:
        logging.error('Unknown protocol: %s', protocol)


def fetchArtifactLists(urlToMAList, outputDir, checksumMode, threadnum):
    """
    Fetch lists of artifacts each list from its repository.
    """
    for repoUrl in urlToMAList.keys():
        artifacts = urlToMAList[repoUrl]
        fetchArtifactList(repoUrl, outputDir, artifacts, checksumMode, threadnum)
