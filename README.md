Maven Repository Builder
========================

Build a Maven repository from a list of dependencies and the URL to an existing repository.
The list can be generated from defined sources like MEAD tag, remote/local repository or a
set of top level artifacts, which dependencies are included. This way there can be combined
several sources. Another way is to provide the artifact list in a file and call the builder
with single source URL. This tool requires python 2.6 or higher.

There is a set of tools in the repository. The complete list of them is:
* **Maven Repository Builder (maven_repo_builder.sh)**
    * builds a Maven repository
    * uses Artifact List Generator and Maven Repository Metadata Generator
* **Maven Repository Comparator (compare_repositories.py)**
    * compares two repositories to see if they contain matching GAVs
* **Artifact List Generator (artifact_list_generator.py)**
    * generates list of artifact and filters it according to config file
* **Maven Repository Metadata Generator (generate_maven_metadata.sh)**
    * generates Maven metadata xml files for usage as a local repository


Maven Repository Builder Usage
------------------------------

    Usage:
        maven_repo_builder.sh -u URL [-r REPO_FILENAME] [-m] [-n] [-o OUTPUT] [-b OUTPUT_REPO] [-a CLASSIFIERS] [-s CHECKSUM_MODE] [-x EXCLUDED_TYPES] [-d ADDITION] FILE...
        or
        maven_repo_builder.sh -c CONFIG [-r REPO_FILENAME] [-m] [-n] [-o OUTPUT] [-b OUTPUT_REPO] [-a CLASSIFIERS] [-s CHECKSUM_MODE] [-x EXCLUDED_TYPES] [-d ADDITION]

    Generate a Maven repository based on a file (or files) containing a list of artifacts.  Each list file must contain
    a single artifact per line in the format groupId:artifactId:fileType:<classifier>:version The example artifact list
    contains more information. Another usage is to provide Artifact List Generator configuration file.

    Options:
      -h                    show this help message and exit
      -c CONFIG
                            Configuration file to use to generate an artifact list
                            for the repository builder
      -u URL
                            Comma-separated list of URLs of the remote repositories from
                            which artifacts are downloaded. It is used along with artifact
                            list files when no config file is specified.
      -o OUTPUT
                            Local output directory for the new repository. By default
                            "local-maven-repository" will be used.
      -b OUTPUT_REPO
                            Name of directory in which will be the artifracts contained in the
                            output direcotry. It can be empty or multi-level path,
                            e.g. path/to/repo. Defaults to "maven-repository".
      -a CLASSIFIERS
                            Comma-separated list of additional classifiers to download.
                            By default "sources" will be used. It is possible to use
                            "__all__" to request all available classifiers (works only when
                            artifact list is generated from config). There can be a type
                            specified with each classifiers separated by colon, e.g. jar:sources.
                            The old way of separation of classifiers by colon is deprecated since.
      -r REPO_FILENAME
                            Zip teh created repository in a file with provided name
      -s CHECKSUM_MODE
                            Mode of dealing with MD5 and SHA1 checksums. Possible options are:
                            generate - generate the checksums (default)
                            download - download the checksums if available, if not, generate them
                            check - check if downloaded and generated checksums are equal
      -x EXCLUDED_TYPES
                            Colon-separated list of filetypes to exclude. Defaults to
                            zip:ear:war:tar:gz:tar.gz:bz2:tar.bz2:7z:tar.7z
      -m
                            Generate metadata in the created repository
      -l LOGLEVEL
                            Set the level of log output.  Can be set to debug,
                            info, warning, error, or critical
      -L LOGFILE
                            Set the file in which the log output should be written
      -d ADDITION
                            Directory containing additional files for the repository.
                            Content of directory ADDITION will be copied to the repository.
      -n
                            Disable producing a nested structure for the maven repository zip,
                            in which the maven-repository directory and the extras files were nested
                            under a parent directory.



### Example Repository List
For a description and examples of the format of the artifact list file, see
[Example Repository List](https://github.com/jboss-eap/maven-repository-builder/blob/master/example-config/artifact-list.txt)


Maven Repository Comparator
---------------------------
A script is also included which can be used to compare two repositories to see if they
contain matching GAVs (artifacts with the same groupId, artifactId, and version).

    Usage: compare_repositories.py [options] REPOSITORY_PATH

    Compare a local Maven repository to a remote repository.

    Options:
      -h, --help            show this help message and exit
      -l LOGLEVEL, --loglevel=LOGLEVEL
                            Set the level of log output.  Can be set to debug,
                            info, warning, error, or critical
      -L LOGFILE, --logfile=LOGFILE
                            Set the file in which the log output should be
                            written.
      -u URL, --url=URL     URL of the remote repository to use for comparison


Artifact List Generator
-----------------------
The Artifact List Generator is a tool which handles generation of artifact list from specified sources. It is used by
maven_repo_builder.py or it can be used as a separate tool. Configuration structure is described below.

    Usage:
        artifact_list_generator.py -c CONFIG

    Generates an artifact list according to provided configuration. The output of the script is a list of repository
    URLs and artifacts separated by tab character per line. Every artifact is in format
    groupId:artifactId:fileType:version. The output is printed on stdout.

    Options:
      -h, --help            show this help message and exit
      -c CONFIG, --config=CONFIG
                            Configuration file to use for generation of an
                            artifact list for the repository builder
      -a CLASSIFIERS, --classifiers=CLASSIFIERS
                            Comma-separated list of additional classifiers to
                            download. It is possible to use "__all__" to request
                            all available classifiers. There can be a type
                            specified with each classifiers separated by colon,
                            e.g. sources:jar.
      -x EXCLUDEDTYPES, --excludedtypes=EXCLUDEDTYPES
                            Colon-separated list of filetypes to exclude. Defaults
                            to zip:ear:war:tar:gz:tar.gz:bz2:tar.bz2:7z:tar.7z.
      --nocache             Don't use any cache (dependency graph cache etc).
      -w WHITELIST, --whitelist=WHITELIST
                            Name of a file containing GATCV patterns allowing
                            usage of stars or regular expressions when enclosed in
                            "r/pattern/". It can force inclusion of artifacts with
                            excluded types.
      -l LOGLEVEL, --loglevel=LOGLEVEL
                            Set the level of log output. Can be set to debug,
                            info, warning, error, or critical
      -L LOGFILE, --logfile=LOGFILE
                            Set the file in which the log output should be
                            written.


### Artifact List Generator Config
For an example config with full config structure see [Sample Config](https://github.com/jboss-eap/maven-repository-builder/blob/master/example-config/alg-config-sample.json)

#### Basic configuration
*   **artifact-sources** - list of sources of artifacts which should be included in the produced repository
    *   **type** - one of the artifact source types:
        *   "mead-tag" - a MEAD tag which should be included. Additional artifact source config fields for this type are:
            *   **tag-name** - the tag name.
            *   **koji-url** - URL of koji (Brew/MEAD) instance, usually
                ["http://brewhub.devel.redhat.com/brewhub/"](http://brewhub.devel.redhat.com/brewhub/).
            *   **download-root-url** - root URL from which build packages should be downloaded, usually
                ["http://download.devel.redhat.com/brewroot/packages/"](http://download.devel.redhat.com/brewroot/packages/).
            *   **included-gav-patterns-ref** - reference to a file, where are patterns of included GAVs, on every line
                should be single pattern. Stars are allowed to represent any string. To use regular expressions prefix
                the expression with "r" and enclose it in slashes ("r/regular-expression/"). The field is not required,
                if ommited, all found artifacts will be included.
        *   "dependency-list" - a merged lists of maven dependencies of selected GAVs. Additional artifact source
            config fields for this type are
            *   **repo-url** - one repository URL or a list of them which should be searched.
            *   **top-level-gavs-ref** - reference to a file with the list of top level GAVs, every GAV should be on
                separate line.
            *   **recursive** - flag to allow listing the listed dependencies recursively. Default is True.
            *   **skip-missing** - flag to skip missing dependencies and only warn user instead of exiting. Default is
                True.
            *   **include-scope** - speicifes scope to use as includeScope parameter when running mvn. For more informations
                on available values see https://maven.apache.org/plugins/maven-dependency-plugin/list-mojo.html#includeScope
                Default value is specified by maven-dependency-plugin.
        *   "dependency-graph" - a merged lists of maven dependency graph of selected GAVs provided by Indy. Additional
            artifact source config fields for this type are
            *   **indy-url** - Indy instance URL (without the API part)
                (e.g. "http://dev208.mw.lab.eng.bos.redhat.com:8080/").
            *   **top-level-gavs-ref** - reference to a file with the list of top level GAVs, every GAV should be on
                separate line.
            *   **source-key** - Indy source key in form of <type>:<name>, e.g. "repository:central".
            *   **wsid** - Indy workspace ID. The field is not required. If ommited, a new workspace will be created
                and deleted after all work finished.
            *   **preset** - Indy preset. This specifies the artifact filter to be used inside Indy. The field is not required.
            *   **preset-params** - Dictionary of Indy preset params. The field is not required.
            *   **mutator** - Indy mutator. This can tell Indy to use or ignore the root artifacts' dependency management.
                The field is not required.
            *   **injected-boms** - list of injected BOMs' GAVs when using dependency management injection Maven extension
            *   **excluded-sources** - list of Indy source keys to be excluded directly in the result of depgrapher.
                The field is not required.
            *   **excluded-subgraphs** - list of GAVs to be excluded along with their subgraphs. The field is not required.
            *   **excluded-subgraphs-ref** - reference to a file with the list of GAVs to be excluded along with their
			    subgraphs. You can use it along with the **excluded-subgraphs** and the values will be merged. The field is
				not required.
            *   **patcher-ids** - list of patcher ID strings for Indy.
        *   "repository" - a local or remote repository to crawl and include found artifacts. Additional artifact
            source config fields for this type are
            *   **repo-url** -one repository URL or a list of them, which should be crawled. For Indy repository manager 
                optimized listing should be used. That can be achieved by using indy/indys as protocol, e.g. 
                indy://server.domain.com/api/content/maven/group/public
            *   **included-gav-patterns-ref** - the same as the corresponding MEAD tag's field (see above)


#### Advanced config
*   **include-high-priority** and **include-low-priority** - includes another config file, which values will be
    overwritten by curernt config or in case of lists they will be merged. If order in a list matters (like in
    **artifact-sources**) then the items from the included file will be added before and after for high and low
    priority includes respectively. Neither of them is required.
*   **excluded-gav-patterns-ref** - list of references to files with list of GAV (or GATCV) patterns to be excluded, every
    pattern should be on separate line. GATCV patterns are recoginized by more than two colons included in the string.
    Stars are allowed to represent any string. To use regular expressions prefix the expression with "r" and enclose it in
    slashes ("r/regular-expression/"). Not required.
*   **excluded-repositories** - list of repository URLs which will be searched for any artifact found in specified
    artifact sources and when found, the artifact will be disposed. Not required.
*   **single-version** - flag to forbid multiple versions of one groupId:artifactId, there can be allowed multiple
    versions for specific GAs by **multi-version-ga-patterns-ref**. Not required, default value is true.
*   **multi-version-ga-patterns-ref** - list of references to a files with lists of GA patterns (stars allowed) with
    permitted multiple versions. Not required, used only when **single-version** = "true".


Maven Repository Metadata Generator
-----------------------------------
There is a script used by Maven Repository Builder for metadata generator. It can be used
separately from the builder.

    Usage: generate_maven_metadata.sh [REPOSITORY_PATH]

    Generates metadata in specified directory. If none is specified then current workdir
    is used. It is needed to pass the repository root as parameter, because it converts
    the relative path to artifacts into group ID, artifact ID and version. If it wasn't
    the repository root, the group ID would be probably wrong or the whole tool would end
    up with an error.
