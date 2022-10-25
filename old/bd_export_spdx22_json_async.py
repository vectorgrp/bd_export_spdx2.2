#!/usr/bin/env python
import argparse
import json
import logging
import sys
import datetime
import os
import re
from lxml import html
import requests
import aiohttp
import asyncio
import time

from blackduck import Client

script_version = "0.13 Async"

logging.basicConfig(format='%(asctime)s:%(levelname)s:%(message)s', stream=sys.stderr, level=logging.INFO)
logging.getLogger("requests").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)

processed_comp_list = []
spdx_custom_lics = []

# The name of a custom attribute which should override the default package supplier
SBOM_CUSTOM_SUPPLIER_NAME = "PackageSupplier"

usage_dict = {
    "SOURCE_CODE": "CONTAINS",
    "STATICALLY_LINKED": "STATIC_LINK",
    "DYNAMICALLY_LINKED": "DYNAMIC_LINK",
    "SEPARATE_WORK": "OTHER",
    "MERELY_AGGREGATED": "OTHER",
    "IMPLEMENTATION_OF_STANDARD": "OTHER",
    "PREREQUISITE": "HAS_PREREQUISITE",
    "DEV_TOOL_EXCLUDED": "DEV_TOOL_OF"
}

matchtype_depends_dict = {
    "FILE_DEPENDENCY_DIRECT": "DEPENDS_ON",
    "FILE_DEPENDENCY_TRANSITIVE": "DEPENDS_ON",
}

matchtype_contains_dict = {
    "FILE_EXACT": "CONTAINS",
    "FILE_FILES_ADDED_DELETED_AND_MODIFIED": "CONTAINS",
    "FILE_DEPENDENCY": "CONTAINS",
    "FILE_EXACT_FILE_MATCH": "CONTAINS",
    "FILE_SOME_FILES_MODIFIED": "CONTAINS",
    "MANUAL_BOM_COMPONENT": "CONTAINS",
    "MANUAL_BOM_FILE": "CONTAINS",
    "PARTIAL_FILE": "CONTAINS",
    "BINARY": "CONTAINS",
    "SNIPPET": "OTHER",
}

spdx_deprecated_dict = {
    'AGPL-1.0': 'AGPL-1.0-only',
    'AGPL-3.0': 'AGPL-3.0-only',
    'BSD-2-Clause-FreeBSD': 'BSD-2-Clause',
    'BSD-2-Clause-NetBSD': 'BSD-2-Clause',
    'eCos-2.0': 'NOASSERTION',
    'GFDL-1.1': 'GFDL-1.1-only',
    'GFDL-1.2': 'GFDL-1.2-only',
    'GFDL-1.3': 'GFDL-1.3-only',
    'GPL-1.0': 'GPL-1.0-only',
    'GPL-1.0+': 'GPL-1.0-or-later',
    'GPL-2.0-with-autoconf-exception': 'GPL-2.0-only',
    'GPL-2.0-with-bison-exception': 'GPL-2.0-only',
    'GPL-2.0-with-classpath-exception': 'GPL-2.0-only',
    'GPL-2.0-with-font-exception': 'GPL-2.0-only',
    'GPL-2.0-with-GCC-exception': 'GPL-2.0-only',
    'GPL-2.0': 'GPL-2.0-only',
    'GPL-2.0+': 'GPL-2.0-or-later',
    'GPL-3.0-with-autoconf-exception': 'GPL-3.0-only',
    'GPL-3.0-with-GCC-exception': 'GPL-3.0-only',
    'GPL-3.0': 'GPL-3.0-only',
    'GPL-3.0+': 'GPL-3.0-or-later',
    'LGPL-2.0': 'LGPL-2.0-only',
    'LGPL-2.0+': 'LGPL-2.0-or-later',
    'LGPL-2.1': 'LGPL-2.1-only',
    'LGPL-2.1+': 'LGPL-2.1-or-later',
    'LGPL-3.0': 'LGPL-3.0-only',
    'LGPL-3.0+': 'LGPL-3.0-or-later',
    'Nunit': 'NOASSERTION',
    'StandardML-NJ': 'SMLNJ',
    'wxWindows': 'NOASSERTION'
}

spdx = dict()
spdx['packages'] = []
spdx['relationships'] = []
spdx['snippets'] = []
spdx['hasExtractedLicensingInfos'] = []

parser = argparse.ArgumentParser(description='"Export SPDX JSON format file for the given project and version"',
                                 prog='bd_export_spdx22_json.py')
parser.add_argument("project_name", type=str, help='Black Duck project name')
parser.add_argument("project_version", type=str, help='Black Duck version name')
parser.add_argument("-v", "--version", help="Print script version and exit", action='store_true')
parser.add_argument("-o", "--output", type=str,
                    help="Output SPDX file name (SPDX JSON format) - default '<proj>-<ver>.json'", default="")
parser.add_argument("-r", "--recursive", help="Scan sub-projects within projects (default = false)",
                    action='store_true')
parser.add_argument("--download_loc",
                    help='''Attempt to identify component download link extracted from Openhub
                    (slows down processing - default=false)''',
                    action='store_true')
parser.add_argument("--no_copyrights",
                    help="Do not export copyright data for components (speeds up processing - default=false)",
                    action='store_true')
parser.add_argument("--no_files",
                    help="Do not export file data for components (speeds up processing - default=false)",
                    action='store_true')
parser.add_argument("-b", "--basic",
                    help='''Do not export copyright, download link  or package file data (speeds up processing -
                    same as using "--download_loc --no_copyrights --no_files")''',
                    action='store_true')
parser.add_argument("--blackduck_url", type=str, help="BLACKDUCK_URL", default="")
parser.add_argument("--blackduck_api_token", type=str, help="BLACKDUCK_API_TOKEN", default="")
parser.add_argument("--blackduck_trust_certs", help="BLACKDUCK trust certs", action='store_true')
parser.add_argument("--blackduck_timeout", help="BD Server requests timeout (seconds - default 15)", default=15)
parser.add_argument("--debug", help="Turn on debug messages", action='store_true')

args = parser.parse_args()

spdx_ids = {}

url = os.environ.get('BLACKDUCK_URL')
if args.blackduck_url:
    url = args.blackduck_url

api = os.environ.get('BLACKDUCK_API_TOKEN')
if args.blackduck_api_token:
    api = args.blackduck_api_token

verify = True
if args.blackduck_trust_certs:
    verify = False

if url == '' or url is None:
    print('BLACKDUCK_URL not set or specified as option --blackduck_url')
    sys.exit(2)

if api == '' or api is None:
    print('BLACKDUCK_API_TOKEN not set or specified as option --blackduck_api_token')
    sys.exit(2)

bd = Client(
    token=api,
    base_url=url,
    verify=verify,  # TLS certificate verification
    timeout=args.blackduck_timeout
)


def clean_for_spdx(name):
    newname = re.sub('[;:!*()/,]', '', name)
    newname = re.sub('[ .]', '', newname)
    newname = re.sub('@', '-at-', newname)
    newname = re.sub('_', 'uu', newname)

    return newname


def quote(name):
    remove_chars = ['"', "'"]
    for i in remove_chars:
        name = name.replace(i, '')
    return name


def get_all_projects():
    global bd
    projs = bd.get_resource('projects', items=True)

    projlist = []
    for proj in projs:
        projlist.append(proj['name'])
    return projlist


def backup_file(filename):
    import os

    if os.path.isfile(filename):
        # Determine root filename so the extension doesn't get longer
        n = os.path.splitext(filename)[0]

        # Is e an integer?
        try:
            root = n
        except ValueError:
            root = filename

        # Find next available file version
        for i in range(1000):
            new_file = "{}.{:03d}".format(root, i)
            if not os.path.isfile(new_file):
                os.rename(filename, new_file)
                print("INFO: Moved old output file '{}' to '{}'\n".format(filename, new_file))
                return new_file
    return ''


def openhub_get_download(oh_url):
    try:
        page = requests.get(oh_url)
        tree = html.fromstring(page.content)

        link = ""
        enlistments = tree.xpath("//a[text()='Project Links:']//following::a[text()='Code Locations:']//@href")
        if len(enlistments) > 0:
            enlist_url = "https://openhub.net" + str(enlistments[0])
            enlist_page = requests.get(enlist_url)
            enlist_tree = html.fromstring(enlist_page.content)
            link = enlist_tree.xpath("//tbody//tr[1]//td[1]/text()")

        if len(link) > 0:
            sp = str(link[0].split(" ")[0]).replace('\n', '')
            #
            # Check format
            protocol = sp.split('://')[0]
            if protocol in ['https', 'http', 'git']:
                return sp

    except Exception as exc:
        print('ERROR: Cannot get openhub data\n' + str(exc))
        return "NOASSERTION"

    return "NOASSERTION"


# 1. translate external_namespace to purl_type [and optionally, purl_namespace]
# 2. split external_id into component_id and version:
#     if: external_namespace not in (npmjs, maven) and splt(external_id by id_separator) > 2 segements
#         split external_id by id_separator on first occurence
#             1: component_id
#             2: version
#     else:
#         split external_id by id_separator on last occurence
#             1: component_id
#             2: version
# 3. purl := "pkg:{:purl_type}"
# 4. if purl_namespace:
#     purl += "/{:purl_namespace}"
# 5. if id_separator in component_id:
#     purl += "/" + 1st part of split(component_id by id_separator)
# 6. if id_separator not in component_id:
#         if external_namespace is pypi
#             then purl += "/" + regexp_replace(lower(component_id), '[-_.]+', '-', 'g')
#             else purl += "/{:component_id}"
#         else
#             purl += "/" + 2nd part of split(component_id by id_separator)
# 7. purl += "@" + 1st part of split(regexp_replace(version, '^\d+:', '') by id_separator)
#    append qualifiers if any:
# 8.    purl += "?"
# 9.    if id_separator in version:
#          then purl += "&arch=" + 2nd part of split(version by id_separator)
# 10.   if version matches /^(\d+):/
#         then purl += "&epoch=" + match_group_1
# 11.   if other qualifier:
#         append uri params
# 12. if subpath is known (i.e. golang import subpath)
#     purl += "#{:subpath}"

def calculate_purl(namespace, extid):
    spdx_origin_map = {
        "alpine": {"p_type": "apk", "p_namespace": "alpine", "p_sep": "/"},
        "android": {"p_type": "apk", "p_namespace": "android", "p_sep": ":"},
        "bitbucket": {"p_type": "bitbucket", "p_namespace": "", "p_sep": ":"},
        "bower": {"p_type": "bower", "p_namespace": "", "p_sep": "/"},
        "centos": {"p_type": "rpm", "p_namespace": "centos", "p_sep": "/"},
        "clearlinux": {"p_type": "rpm", "p_namespace": "clearlinux", "p_sep": "/"},
        "cpan": {"p_type": "cpan", "p_namespace": "", "p_sep": "/"},
        "cran": {"p_type": "cran", "p_namespace": "", "p_sep": "/"},
        "crates": {"p_type": "cargo", "p_namespace": "", "p_sep": "/"},
        "dart": {"p_type": "pub", "p_namespace": "", "p_sep": "/"},
        "debian": {"p_type": "deb", "p_namespace": "debian", "p_sep": "/"},
        "fedora": {"p_type": "rpm", "p_namespace": "fedora", "p_sep": "/"},
        "gitcafe": {"p_type": "gitcafe", "p_namespace": "", "p_sep": ":"},
        "github": {"p_type": "github", "p_namespace": "", "p_sep": ":"},
        "gitlab": {"p_type": "gitlab", "p_namespace": "", "p_sep": ":"},
        "gitorious": {"p_type": "gitorious", "p_namespace": "", "p_sep": ":"},
        "golang": {"p_type": "golang", "p_namespace": "", "p_sep": ":"},
        "hackage": {"p_type": "hackage", "p_namespace": "", "p_sep": "/"},
        "hex": {"p_type": "hex", "p_namespace": "", "p_sep": "/"},
        "maven": {"p_type": "maven", "p_namespace": "", "p_sep": ":"},
        "mongodb": {"p_type": "rpm", "p_namespace": "mongodb", "p_sep": "/"},
        "npmjs": {"p_type": "npm", "p_namespace": "", "p_sep": "/"},
        "nuget": {"p_type": "nuget", "p_namespace": "", "p_sep": "/"},
        "opensuse": {"p_type": "rpm", "p_namespace": "opensuse", "p_sep": "/"},
        "oracle_linux": {"p_type": "rpm", "p_namespace": "oracle", "p_sep": "/"},
        "packagist": {"p_type": "composer", "p_namespace": "", "p_sep": ":"},
        "pear": {"p_type": "pear", "p_namespace": "", "p_sep": "/"},
        "photon": {"p_type": "rpm", "p_namespace": "photon", "p_sep": "/"},
        "pypi": {"p_type": "pypi", "p_namespace": "", "p_sep": "/"},
        "redhat": {"p_type": "rpm", "p_namespace": "redhat", "p_sep": "/"},
        "ros": {"p_type": "deb", "p_namespace": "ros", "p_sep": "/"},
        "rubygems": {"p_type": "gem", "p_namespace": "", "p_sep": "/"},
        "ubuntu": {"p_type": "deb", "p_namespace": "ubuntu", "p_sep": "/"},
        "yocto": {"p_type": "yocto", "p_namespace": "", "p_sep": "/"},
    }

    if namespace in spdx_origin_map.keys():
        ns_split = extid.split(spdx_origin_map[namespace]['p_sep'])
        if namespace not in ['npmjs', 'maven'] and len(ns_split) > 2:  # 2
            compid, compver = extid.split(spdx_origin_map[namespace]['p_sep'], maxsplit=1)
        elif spdx_origin_map[namespace]['p_sep'] in extid:
            compid, compver = extid.rsplit(spdx_origin_map[namespace]['p_sep'], maxsplit=1)
        else:
            compid, compver = extid, None

        purl = "pkg:" + spdx_origin_map[namespace]['p_type']  # 3

        if spdx_origin_map[namespace]['p_namespace'] != '':  # 4
            purl += "/" + spdx_origin_map[namespace]['p_namespace']

        if spdx_origin_map[namespace]['p_sep'] in compid:  # 5
            purl += '/' + '/'.join(quote(s) for s in compid.split(spdx_origin_map[namespace]['p_sep']))
        else:  # 6
            if namespace == 'pypi':
                purl += '/' + quote(re.sub('[-_.]+', '-', compid.lower()))
            else:
                purl += '/' + quote(compid)

        qual = {}
        if compver:
            if spdx_origin_map[namespace]['p_sep'] in compver:  # 9
                compver, qual['arch'] = compver.split(spdx_origin_map[namespace]['p_sep'])

            purl += '@' + quote(re.sub("^\d+:", '', compver))  # 7

            epoch_m = re.match('^(\d+):', compver)  # 10
            if epoch_m:
                qual['epoch'] = epoch_m[1]

        if qual:
            purl += '?' + '&'.join('='.join([k, quote(v)]) for k, v in qual.items())  # 8

        return purl
    return ''


def get_package_supplier(comp):
    
    fields_val = bd.get_resource('custom-fields', comp)
    
    sbom_field = next((item for item in fields_val if item['label'] == SBOM_CUSTOM_SUPPLIER_NAME), None)
    
    if sbom_field is not None and len(sbom_field['values']) > 0:
       supplier_name = sbom_field['values'][0]
       return supplier_name
    
    return


def process_comp(comps_dict, tcomp, comp_data_dict):
    # global output_dict
    global bom_components
    global spdx_custom_lics
    global spdx
    global processed_comp_list

    cver = tcomp['componentVersion']
    if cver in comps_dict.keys():
        # ind = compverlist.index(tcomp['componentVersion'])
        bomentry = comps_dict[cver]
    else:
        bomentry = tcomp

    spdxpackage_name = clean_for_spdx(
        "SPDXRef-Package-" + tcomp['componentName'] + "-" + tcomp['componentVersionName'])

    if spdxpackage_name in spdx_ids:
        return spdxpackage_name

    spdx_ids[spdxpackage_name] = 1

    openhub_url = None

    if cver not in processed_comp_list:
        download_url = "NOASSERTION"

        fcomp = bd.get_json(tcomp['component'])  #  CHECK THIS
        #
        openhub_url = next((item for item in bomentry['_meta']['links'] if item["rel"] == "openhub"), None)
        if args.download_loc and openhub_url is not None:
                download_url = openhub_get_download(openhub_url['href'])

        copyrights = "NOASSERTION"
        cpe = "NOASSERTION"
        pkg = "NOASSERTION"
        if not args.no_copyrights:
            # copyrights, cpe, pkg = get_orig_data(bomentry)
            copyrights = comp_data_dict[cver]['copyrights']

            if 'origins' in bomentry.keys() and len(bomentry['origins']) > 0:
                orig = bomentry['origins'][0]
                if 'externalNamespace' in orig.keys() and 'externalId' in orig.keys():
                    pkg = calculate_purl(orig['externalNamespace'], orig['externalId'])

        package_file = "NOASSERTION"
        if not args.no_files:
            package_file = comp_data_dict[cver]['files']

        desc = 'NOASSERTION'
        if 'description' in tcomp.keys():
            desc = re.sub('[^a-zA-Z.()\d\s\-:]', '', bomentry['description'])

        annotations = comp_data_dict[cver]['comments']
        lic_string = comp_data_dict[cver]['licenses']

        component_package_supplier = ''

        homepage = 'NOASSERTION'
        if 'url' in fcomp.keys():
            homepage = fcomp['url']  # CHECK THIS

        bom_package_supplier = get_package_supplier(bomentry)

        packageinfo = "This is a"

        if bomentry['componentType'] == 'CUSTOM_COMPONENT':
            packageinfo = packageinfo + " custom component"
        if bomentry['componentType'] == 'SUB_PROJECT':
            packageinfo = packageinfo + " sub project"
        else:
            packageinfo = packageinfo + "n open source component from the Black Duck Knowledge Base"

        if len(bomentry['matchTypes']) > 0:
            firstType = bomentry['matchTypes'][0]
            if(firstType == 'MANUAL_BOM_COMPONENT'):
                packageinfo = packageinfo + " which was manually added"
            else:
                packageinfo = packageinfo + " which was automatically detected"
                if firstType == 'FILE_EXACT':
                    packageinfo = packageinfo + " as a direct file match"
                elif firstType == 'SNIPPET':
                    packageinfo = packageinfo + " as a code snippet"
                elif firstType == 'FILE_DEPENDENCY_DIRECT':
                    packageinfo = packageinfo + " as a directly declared dependency"
                elif firstType == 'FILE_DEPENDENCY_TRANSITIVE':
                    packageinfo = packageinfo + " as a transitive dependency"

        packagesuppliername = ''

        if bom_package_supplier is not None and len(bom_package_supplier) > 0:
            packageinfo = packageinfo + ", the PackageSupplier was provided by the user at the BOM level"
            packagesuppliername = packagesuppliername + bom_package_supplier
            pkg = "supplier:{}/{}/{}".format(bom_package_supplier.replace("Organization: ", ""), tcomp['componentName'], tcomp['componentVersionName'])
        elif component_package_supplier is not None and len(component_package_supplier) > 0:
            packageinfo = packageinfo + ", the PackageSupplier was populated in the component"
            packagesuppliername = packagesuppliername + component_package_supplier
            pkg = "supplier:{}/{}/{}".format(component_package_supplier.replace("Organization: ", ""), tcomp['componentName'], tcomp['componentVersionName'])
        elif bomentry['origins'] is not None and len(bomentry['origins']) > 0:
            packagesuppliername = packagesuppliername + "Organization: " + bomentry['origins'][0]['externalNamespace']
            packageinfo = packageinfo + ", the PackageSupplier was based on the externalNamespace"
        else:
            packageinfo = packageinfo + ", the PackageSupplier was not populated"
            packagesuppliername = packagesuppliername + "NOASSERTION"

        # TO DO - use packagesuppliername somewhere

        thisdict = {
            "SPDXID": quote(spdxpackage_name),
            "name": quote(tcomp['componentName']),
            "versionInfo": quote(tcomp['componentVersionName']),
            "packageFileName": quote(package_file),
            "description": quote(desc),
            "downloadLocation": quote(download_url),
            "packageHomepage": quote(homepage),
            # PackageChecksum: SHA1: 85ed0817af83a24ad8da68c2b5094de69833983c,
            "licenseConcluded": quote(lic_string),
            "licenseDeclared": quote(lic_string),
            "packageSupplier": packagesuppliername,
            # PackageLicenseComments: <text>Other versions available for a commercial license</text>,
            "filesAnalyzed": False,
            "packageComment": quote(packageinfo),
            # "ExternalRef: SECURITY cpe23Type {}".format(cpe),
            # "ExternalRef: PACKAGE-MANAGER purl pkg:" + pkg,
            # ExternalRef: PERSISTENT-ID swh swh:1:cnt:94a9ed024d3859793618152ea559a168bbcbb5e2,
            # ExternalRef: OTHER LocationRef-acmeforge acmecorp/acmenator/4.1.3-alpha,
            # ExternalRefComment: This is the external ref for Acme,
            "copyrightText": quote(copyrights),
            "annotations": annotations,
        }

        if pkg != '':
            thisdict["externalRefs"] = [
                {
                    "referenceLocator": pkg,
                    "referenceCategory": "PACKAGE_MANAGER",
                    "referenceType": "purl"
                },
                {
                    "referenceCategory": "OTHER",
                    "referenceType": "BlackDuckHub-Component",
                    "referenceLocator": tcomp['component'],
                },
                {
                    "referenceCategory": "OTHER",
                    "referenceType": "BlackDuckHub-Component-Version",
                    "referenceLocator": cver
                }
            ]
            if openhub_url is not None:
                thisdict['externalRefs'].append({
                    "referenceCategory": "OTHER",
                    "referenceType": "OpenHub",
                    "referenceLocator": openhub_url
                })

        spdx['packages'].append(thisdict)
    return spdxpackage_name


def process_children(pkgname, compverurl, child_url, indenttext, comps_dict, comp_data_dict):
    global spdx_custom_lics
    global processed_comp_list

    res = bd.get_json(child_url + '?limit=5000')

    count = 0
    for child in res['items']:
        if 'componentName' not in child or 'componentVersionName' not in child:
            # print("{}{}/{}".format(indenttext, child['componentName'], child['componentVersionName']))
        # else:
            # No version - skip
            print("{}{}/{} (SKIPPED)".format(indenttext, child['componentName'], '?'))
            continue

        childpkgname = process_comp(comps_dict, child, comp_data_dict)
        count += 1
        if childpkgname != '':
            reln = False
            for tchecktype in matchtype_depends_dict.keys():
                if tchecktype in child['matchTypes']:
                    add_relationship(pkgname, childpkgname, matchtype_depends_dict[tchecktype])
                    reln = True
                    break
            if not reln:
                for tchecktype in matchtype_contains_dict.keys():
                    if tchecktype in child['matchTypes']:
                        add_relationship(pkgname, childpkgname,
                                         matchtype_contains_dict[tchecktype])
                        break
            processed_comp_list.append(child['componentVersion'])
        else:
            pass

        if len(child['_meta']['links']) > 2:
            thisref = [d['href'] for d in child['_meta']['links'] if d['rel'] == 'children']
            count += process_children(childpkgname, child['componentVersion'], thisref[0], "    " + indenttext,
                                      comps_dict, comp_data_dict)

    return count


def process_comp_relationship(parentname, childname, mtypes):
    global matchtype_depends_dict

    reln = False
    for tchecktype in matchtype_depends_dict.keys():
        if tchecktype in mtypes:
            add_relationship(parentname, childname, matchtype_depends_dict[tchecktype])
            reln = True
            break
    if not reln:
        for tchecktype in matchtype_contains_dict.keys():
            if tchecktype in mtypes:
                add_relationship(parentname, childname, matchtype_contains_dict[tchecktype])
                break


def process_project(project, version, projspdxname, hcomps, bearer_token):
    global proj_list
    global spdx
    global spdx_custom_lics
    global processed_comp_list

    # project, version = check_projver(proj, ver)

    start_time = time.time()
    print('Getting components ... ', end='')
    bom_compsdict = get_bom_components(version)
    print("({})".format(str(len(bom_compsdict))))
    if args.debug:
        print("--- %s seconds ---" % (time.time() - start_time))

    comp_data_dict = asyncio.run(async_main(bom_compsdict, bearer_token, version))
    if args.debug:
        print("--- %s seconds ---" % (time.time() - start_time))

    #
    # Process hierarchical BOM elements
    print('Processing hierarchical BOM ...')
    start_time = time.time()
    compcount = 0
    for hcomp in hcomps:
        if 'componentVersionName' in hcomp:
            compname = "{}/{}".format(hcomp['componentName'], hcomp['componentVersionName'])
            if args.debug:
                print(compname)
        else:
            print("{}/? - (no version - skipping)".format(hcomp['componentName']))
            continue

        pkgname = process_comp(bom_compsdict, hcomp, comp_data_dict)

        if pkgname != '':
            process_comp_relationship(projspdxname, pkgname, hcomp['matchTypes'])
            processed_comp_list.append(hcomp['componentVersion'])
            compcount += 1

            href = [d['href'] for d in hcomp['_meta']['links'] if d['rel'] == 'children']
            if len(href) > 0:
                compcount += process_children(pkgname, hcomp['componentVersion'], href[0], "--> ", bom_compsdict,
                             comp_data_dict)

    print('Processed {} hierarchical components'.format(compcount))
    if args.debug:
        print("--- %s seconds ---" % (time.time() - start_time))

    #
    # Process all entries to find entries not in hierarchical BOM and sub-projects
    print('Processing other components ...')
    start_time = time.time()
    compcount = 0
    for key, bom_component in bom_compsdict.items():
        if 'componentVersion' not in bom_component.keys():
            print(
                "INFO: Skipping component {} which has no assigned version".format(bom_component['componentName']))
            continue

        compname = bom_component['componentName'] + "/" + bom_component['componentVersionName']
        if bom_component['componentVersion'] in processed_comp_list:
            continue
        # Check if this component is a sub-project
        # if bom_component['matchTypes'][0] == "MANUAL_BOM_COMPONENT":
        if args.debug:
            print(compname)

        pkgname = process_comp(bom_compsdict, bom_component, comp_data_dict)
        compcount += 1

        process_comp_relationship(projspdxname, pkgname, bom_component['matchTypes'])

        if args.recursive and bom_component['componentName'] in proj_list:
            #
            # Need to check if this component is a sub-project
            params = {
                'q': "name:" + bom_component['componentName'],
            }
            sub_projects = bd.get_resource('projects', params=params)
            for sub_proj in sub_projects:
                params = {
                    'q': "versionName:" + bom_component['componentVersionName'],
                }
                sub_versions = bd.get_resource('versions', parent=sub_proj, params=params)
                for sub_ver in sub_versions:
                    print("Processing project within project '{}'".format(
                        bom_component['componentName'] + '/' + bom_component['componentVersionName']))

                    res = bd.list_resources(parent=sub_ver)
                    if 'components' in res:
                        sub_comps = bd.get_resource('components', parent=sub_ver)
                    else:
                        thishref = res['href'] + "/components?limit=2000"
                        headers = {
                            'accept': "application/vnd.blackducksoftware.bill-of-materials-6+json",
                        }
                        res2 = bd.get_json(thishref, headers=headers)
                        sub_comps = res2['items']

                    if 'hierarchical-components' in res:
                        sub_hierarchical_bom = bd.get_resource('hierarchical-components', parent=sub_ver)
                    else:
                        thishref = res['href'] + "/hierarchical-components?limit=2000"
                        headers = {
                            'accept': "application/vnd.blackducksoftware.bill-of-materials-6+json",
                        }
                        res2 = bd.get_json(thishref, headers=headers)
                        sub_hierarchical_bom = res2['items']

                    subprojspdxname = clean_for_spdx(bom_component['componentName'] + '/' +
                                                     bom_component['componentVersionName'])
                    # subproj_compsdict = get_bom_components(sub_ver)
                    # subproj_comp_data_dict = asyncio.run(async_main(subproj_compsdict, bearer_token, res['href']))
                    subproj, subver = check_projver(bom_component['componentName'],
                                                    bom_component['componentVersionName'])
                    compcount += process_project(subproj, subver,
                                                 subprojspdxname, sub_hierarchical_bom, bearer_token)
                    break
                break

    print('Processed {} other components'.format(compcount))
    if args.debug:
        print("--- %s seconds ---" % (time.time() - start_time))

    # print('Output {} Overall components'.format(len(processed_comp_list)))

    return compcount


def add_snippet():
    # "snippets": [{
    # 	"SPDXID": "SPDXRef-Snippet",
    # 	"comment": "This snippet was identified as significant and highlighted in this Apache-2.0 file, when a
    # 	commercial scanner identified it as being derived from file foo.c in package xyz which is licensed under
    # 	GPL-2.0.",
    # 	"copyrightText": "Copyright 2008-2010 John Smith",
    # 	"licenseComments": "The concluded license was taken from package xyz, from which the snippet was copied
    # 	into the current file. The concluded license information was found in the COPYING.txt file in package xyz.",
    # 	"licenseConcluded": "GPL-2.0-only",
    # 	"licenseInfoInSnippets": ["GPL-2.0-only"],
    # 	"name": "from linux kernel",
    # 	"ranges": [{
    # 		"endPointer": {
    # 			"lineNumber": 23,
    # 			"reference": "SPDXRef-DoapSource"
    # 		},
    # 		"startPointer": {
    # 			"lineNumber": 5,
    # 			"reference": "SPDXRef-DoapSource"
    # 		}
    # 	}, {
    # 		"endPointer": {
    # 			"offset": 420,
    # 			"reference": "SPDXRef-DoapSource"
    # 		},
    # 		"startPointer": {
    # 			"offset": 310,
    # 			"reference": "SPDXRef-DoapSource"
    # 		}
    # 	}],
    # 	"snippetFromFile": "SPDXRef-DoapSource"
    # }],
    pass


def add_relationship(parent, child, reln):
    global spdx

    mydict = {
        "spdxElementId": quote(parent),
        "relationshipType": quote(reln),
        "relatedSpdxElement": quote(child)
    }
    spdx['relationships'].append(mydict)


def check_params():
    global args

    if args.version:
        print("Script version: " + script_version)
        sys.exit(0)

    if args.basic:
        args.download_loc = False
        args.no_copyrights = True
        args.no_files = True
    if args.output == "":
        args.output = clean_for_spdx(args.project_name + "-" + args.project_version) + ".json"

    if args.output and os.path.exists(args.output):
        backup_file(args.output)


def check_projver(proj, ver):
    params = {
        'q': "name:" + proj,
        'sort': 'name',
    }

    projects = bd.get_resource('projects', params=params)
    for p in projects:
        if p['name'] == proj:
            versions = bd.get_resource('versions', parent=p, params=params)
            for v in versions:
                if v['versionName'] == ver:
                    return p, v
            break
    else:
        print("Version '{}' does not exist in project '{}'".format(ver, proj))
        sys.exit(2)

    print("Project '{}' does not exist".format(proj))
    print('Available projects:')
    projects = bd.get_resource('projects')
    for proj in projects:
        print(proj['name'])
    sys.exit(2)


def get_bom_components(verdict):
    comp_dict = {}
    res = bd.list_resources(verdict)
    # if 'components' not in res:
    if True:
        # Getting the component list via a request is much quicker than the new Client model
        thishref = res['href'] + "/components?limit=5000"
        headers = {
            'accept': "application/vnd.blackducksoftware.bill-of-materials-6+json",
        }
        res = bd.get_json(thishref, headers=headers)
        bom_comps = res['items']
    # else:
    #     bom_comps = bd.get_resource('components', parent=ver)
    for comp in bom_comps:
        if 'componentVersion' not in comp:
            continue
        compver = comp['componentVersion']

        comp_dict[compver] = comp

    return comp_dict


async def async_main(compsdict, token, ver):
    async with aiohttp.ClientSession(trust_env=True) as session:
        copyright_tasks = []
        comment_tasks = []
        file_tasks = []
        lic_tasks = []
        child_tasks = []
        for url, comp in compsdict.items():
            if args.debug:
                print(comp['componentName'] + '/' + comp['componentVersionName'])
            copyright_task = asyncio.ensure_future(async_get_copyrights(session, comp, token))
            copyright_tasks.append(copyright_task)

            comment_task = asyncio.ensure_future(async_get_comments(session, comp, token))
            comment_tasks.append(comment_task)

            file_task = asyncio.ensure_future(async_get_files(session, comp, token))
            file_tasks.append(file_task)

            lic_task = asyncio.ensure_future(async_get_licenses(session, comp, token))
            lic_tasks.append(lic_task)

            # child_task = asyncio.ensure_future(async_get_children(session, ver, comp, token))
            # child_tasks.append(child_task)

        print('Getting component data ... ')
        all_copyrights = dict(await asyncio.gather(*copyright_tasks))
        all_comments = dict(await asyncio.gather(*comment_tasks))
        all_files = dict(await asyncio.gather(*file_tasks))
        all_lics = dict(await asyncio.gather(*lic_tasks))
        # all_children = dict(await asyncio.gather(*child_tasks))
        await asyncio.sleep(0.250)

    comp_data_dict = {}
    for cvurl in compsdict.keys():
        comp_data_dict[cvurl] = {
            'copyrights': all_copyrights[cvurl],
            'comments': all_comments[cvurl],
            'files': all_files[cvurl],
            'licenses': all_lics[cvurl],
        }
    return comp_data_dict


async def async_get_copyrights(session, comp, token):
    global verify
    if not verify:
        ssl = False
    else:
        ssl = None

    copyrights = "NOASSERTION"
    if len(comp['origins']) < 1:
        return comp['componentVersion'], copyrights

    orig = comp['origins'][0]
    link = next((item for item in orig['_meta']['links'] if item["rel"] == "component-origin-copyrights"), None)
    thishref = link['href'] + "?limit=100"
    headers = {
        'accept': "application/vnd.blackducksoftware.copyright-4+json",
        'Authorization': f'Bearer {token}',
    }
    # resp = bd.get_json(thishref, headers=headers)
    async with session.get(thishref, headers=headers, ssl=ssl) as resp:
        result_data = await resp.json()
        for copyrt in result_data['items']:
            if copyrt['active']:
                thiscr = copyrt['updatedCopyright'].splitlines()[0].strip()
                if thiscr not in copyrights:
                    if copyrights == "NOASSERTION":
                        copyrights = thiscr
                    else:
                        copyrights += "\n" + thiscr
    return comp['componentVersion'], copyrights


async def async_get_comments(session, comp, token):
    global verify
    if not verify:
        ssl = False
    else:
        ssl = None

    annotations = []
    hrefs = comp['_meta']['links']

    link = next((item for item in hrefs if item["rel"] == "comments"), None)
    if link:
        thishref = link['href']
        headers = {
            'Authorization': f'Bearer {token}',
            'accept': "application/vnd.blackducksoftware.bill-of-materials-6+json",
        }
        # resp = bd.get_json(thishref, headers=headers)
        async with session.get(thishref, headers=headers, ssl=ssl) as resp:
            result_data = await resp.json()
            mytime = datetime.datetime.now()
            mytime.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            for comment in result_data['items']:
                annotations.append(
                    {
                        "annotationDate": quote(mytime.strftime("%Y-%m-%dT%H:%M:%S.%fZ")),
                        "annotationType": "OTHER",
                        "annotator": quote("Person: " + comment['user']['email']),
                        "comment": quote(comment['comment']),
                    }
                )
    return comp['componentVersion'], annotations


async def async_get_files(session, comp, token):
    global verify
    if not verify:
        ssl = False
    else:
        ssl = None

    retfile = "NOASSERTION"
    hrefs = comp['_meta']['links']

    link = next((item for item in hrefs if item["rel"] == "matched-files"), None)
    if link:
        thishref = link['href']
        headers = {
            'Authorization': f'Bearer {token}',
            'accept': "application/vnd.blackducksoftware.bill-of-materials-6+json",
        }

        async with session.get(thishref, headers=headers, ssl=ssl) as resp:
            result_data = await resp.json()
            cfile = result_data['items']
            if len(cfile) > 0:
                rfile = cfile[0]['filePath']['path']
                for ext in ['.jar', '.ear', '.war', '.zip', '.gz', '.tar', '.xz', '.lz', '.bz2', '.7z', '.rar', '.rar',
                    '.cpio', '.Z', '.lz4', '.lha', '.arj', '.rpm', '.deb', '.dmg', '.gz', '.whl']:
                    if rfile.endswith(ext):
                        retfile = rfile
    return comp['componentVersion'], retfile


async def async_get_licenses(session, lcomp, token):
    global verify
    if not verify:
        ssl = False
    else:
        ssl = None

    global spdx_custom_lics

    # Get licenses
    lic_string = "NOASSERTION"
    quotes = False
    if 'licenses' in lcomp.keys():
        proc_item = lcomp['licenses']

        if len(proc_item[0]['licenses']) > 1:
            proc_item = proc_item[0]['licenses']

        for lic in proc_item:
            thislic = ''
            if 'spdxId' in lic:
                thislic = lic['spdxId']
                if thislic in spdx_deprecated_dict.keys():
                    thislic = spdx_deprecated_dict[thislic]
            else:
                # Custom license
                try:
                    thislic = 'LicenseRef-' + clean_for_spdx(lic['licenseDisplay'])
                    lic_ref = lic['license'].split("/")[-1]
                    headers = {
                        'accept': "text/plain",
                        'Authorization': f'Bearer {token}',
                    }
                    # resp = bd.session.get('/api/licenses/' + lic_ref + '/text', headers=headers)
                    thishref = f"{bd.base_url}/api/licenses/{lic_ref}/text"
                    async with session.get(thishref, headers=headers, ssl=ssl) as resp:
                        lic_text = await resp.content.decode("utf-8")
                        if thislic not in spdx_custom_lics:
                            mydict = {
                                'licenseID': quote(thislic),
                                'extractedText': quote(lic_text)
                            }
                            spdx["hasExtractedLicensingInfos"].append(mydict)
                            spdx_custom_lics.append(thislic)
                except Exception as exc:
                    pass
            if lic_string == "NOASSERTION":
                lic_string = thislic
            else:
                lic_string = lic_string + " AND " + thislic
                quotes = True

        if quotes:
            lic_string = "(" + lic_string + ")"

    return lcomp['componentVersion'], lic_string


def run():
    global spdx
    global args
    global processed_comp_list
    global proj_list

    print("BLACK DUCK SPDX EXPORT SCRIPT VERSION {}\n".format(script_version))

    check_params()

    project, version = check_projver(args.project_name, args.project_version)
    print("Working on project '{}' version '{}'\n".format(project['name'], version['versionName']))

    bearer_token = bd.session.auth.bearer_token

    if args.recursive:
        proj_list = get_all_projects()

    spdx_custom_lics = []

    toppackage = clean_for_spdx("SPDXRef-Package-" + project['name'] + "-" + version['versionName'])

    # Define TOP Document entries
    spdx["SPDXID"] = "SPDXRef-DOCUMENT"
    spdx["spdxVersion"] = "SPDX-2.2"
    spdx["creationInfo"] = {
        "created": quote(version['createdAt'].split('.')[0] + 'Z'),
        "creators": ["Tool: Black Duck SPDX export script https://github.com/matthewb66/bd_export_spdx2.2"],
        "licenseListVersion": "3.9",
    }
    if 'description' in project.keys():
        spdx["creationInfo"]["comment"] = quote(project['description'])
    spdx["name"] = quote(project['name'] + '/' + version['versionName'])
    spdx["dataLicense"] = "CC0-1.0"
    spdx["documentDescribes"] = [toppackage]
    spdx["documentNamespace"] = version['_meta']['href']
    spdx["downloadLocation"] = "NOASSERTION"
    spdx["filesAnalyzed"] = False
    spdx["copyrightText"] = "NOASSERTION"
    spdx["externalRefs"] = [
                {
                    "referenceCategory": "OTHER",
                    "referenceType": "BlackDuckHub-Project",
                    "referenceLocator": project["_meta"]["href"],
                },
                {
                    "referenceCategory": "OTHER",
                    "referenceType": "BlackDuckHub-Project-Version",
                    "referenceLocator": version["_meta"]["href"]
                }
            ]

    add_relationship("SPDXRef-DOCUMENT", toppackage, "DESCRIBES")
    # Add top package for project version
    #
    projpkg = {
        "SPDXID": quote(toppackage),
        "name": quote(project['name']),
        "versionInfo": quote(version['versionName']),
        # "packageFileName":  quote(package_file),
        "licenseConcluded": "NOASSERTION",
        "licenseDeclared": "NOASSERTION",
        "downloadLocation": "NOASSERTION",
        "packageComment": "Generated top level package representing Black Duck project",
        # PackageChecksum: SHA1: 85ed0817af83a24ad8da68c2b5094de69833983c,
        # "licenseConcluded": quote(lic_string),
        # "licenseDeclared": quote(lic_string),
        # PackageLicenseComments: <text>Other versions available for a commercial license</text>,
        "filesAnalyzed": False,
        # "ExternalRef: SECURITY cpe23Type {}".format(cpe),
        # "ExternalRef: PACKAGE-MANAGER purl pkg:" + pkg,
        # ExternalRef: PERSISTENT-ID swh swh:1:cnt:94a9ed024d3859793618152ea559a168bbcbb5e2,
        # ExternalRef: OTHER LocationRef-acmeforge acmecorp/acmenator/4.1.3-alpha,
        # ExternalRefComment: This is the external ref for Acme,
        "copyrightText": "NOASSERTION",
        # annotations,
    }
    if 'description' in project.keys():
        projpkg["description"] = quote(project['description'])
    if 'license' in version.keys():
        if version['license']['licenseDisplay'] == 'Unknown License':
            projpkg["licenseDeclared"] = "NOASSERTION"
        else:
            projpkg["licenseDeclared"] = version['license']['licenseDisplay']
    spdx['packages'].append(projpkg)

    if 'hierarchical-components' in bd.list_resources(version):
        hierarchical_bom = bd.get_resource('hierarchical-components', parent=version)
    else:
        hierarchical_bom = []

    process_project(project, version, toppackage, hierarchical_bom, bearer_token)

    print("Done\n\nWriting SPDX output file {} ... ".format(args.output), end='')

    try:
        with open(args.output, 'w') as outfile:
            json.dump(spdx, outfile, indent=4, sort_keys=True)

    except Exception as e:
        print('ERROR: Unable to create output report file \n' + str(e))
        sys.exit(3)

    print("Done")


if __name__ == "__main__":
    run()
