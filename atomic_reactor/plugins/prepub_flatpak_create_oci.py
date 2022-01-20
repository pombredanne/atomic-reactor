"""
Copyright (c) 2017 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.

Takes the filesystem image created by the Dockerfile generated by
pre_flatpak_create/update_dockerfile, extracts the tree at /var/tmp/flatpak-build
and turns it into a Flatpak application or runtime.
"""

import json
import subprocess

from flatpak_module_tools.flatpak_builder import FlatpakBuilder, FLATPAK_METADATA_ANNOTATIONS

from atomic_reactor.constants import IMAGE_TYPE_OCI, IMAGE_TYPE_OCI_TAR
from atomic_reactor.plugin import PrePublishPlugin
from atomic_reactor.plugins.exit_remove_built_image import defer_removal
from atomic_reactor.plugins.pre_flatpak_update_dockerfile import get_flatpak_source_info
from atomic_reactor.utils import retries
from atomic_reactor.utils.rpm import parse_rpm_output
from atomic_reactor.util import df_parser, get_exported_image_metadata, is_flatpak_build
from osbs.utils import Labels


# This converts the generator provided by the export() operation to a file-like
# object with a read that we can pass to tarfile.
class StreamAdapter(object):
    def __init__(self, gen):
        self.gen = gen
        self.buf = None
        self.pos = None

    def read(self, count):
        pieces = []
        remaining = count
        while remaining > 0:
            if not self.buf:
                try:
                    self.buf = next(self.gen)
                    self.pos = 0
                except StopIteration:
                    break

            if len(self.buf) - self.pos < remaining:
                pieces.append(self.buf[self.pos:])
                remaining -= (len(self.buf) - self.pos)
                self.buf = None
                self.pos = None
            else:
                pieces.append(self.buf[self.pos:self.pos + remaining])
                self.pos += remaining
                remaining = 0

        return b''.join(pieces)

    def close(self):
        pass


class FlatpakCreateOciPlugin(PrePublishPlugin):
    key = 'flatpak_create_oci'
    is_allowed_to_fail = False

    def __init__(self, workflow):
        """
        :param workflow: DockerBuildWorkflow instance
        """
        super(FlatpakCreateOciPlugin, self).__init__(workflow)
        self.builder = None

        try:
            self.flatpak_metadata = self.workflow.conf.flatpak_metadata
        except KeyError:
            self.flatpak_metadata = FLATPAK_METADATA_ANNOTATIONS

#    def _export_container(self, container_id):
#        export_generator = self.tasker.export_container(container_id)
#        export_stream = StreamAdapter(export_generator)
#
#        outfile, manifestfile = self.builder._export_from_stream(export_stream)
#
#        return outfile, manifestfile

    def _export_filesystem(self):
        # image = self.workflow.image
        self.log.info("Creating temporary docker container")
        # The command here isn't used, since we only use the container for export,
        # but (in some circumstances) the docker daemon will error out if no
        # command is specified.

        # no longer create and export container, just use oc image extract
        # to get content of the image
        # OSBS2 TBD
        # container_dict = self.tasker.create_container(image, command=["/bin/bash"])
        # container_id = container_dict['Id']

        # try:
        #     return self._export_container(container_id)
        # finally:
        #     self.log.info("Cleaning up docker container")
        #     self.tasker.remove_container(container_id)
        return None, None

    def _get_oci_image_id(self, oci_path):
        cmd = ['skopeo', 'inspect', '--raw', 'oci:{}'.format(oci_path)]
        raw_manifest = retries.run_cmd(cmd)
        oci_image_manifest = json.loads(raw_manifest)
        return oci_image_manifest['config']['digest']

    def _copy_oci_to_local_storage(self, oci_path, name, tag):
        """Copy OCI image to internal container storage

        The internal storage to copy the image to is defined by the workflow.

        :param oci_path: str, path to OCI directory
        :param name: str, name to be given to the image in the internal storage
        :param tag: str, tag to apply to name in the internal storage
        """
        skopeo_copy_dst = '{}:{}:{}'.format(self.workflow.storage_transport, name, tag)
        cmd = ['skopeo', 'copy', 'oci:{}'.format(oci_path), skopeo_copy_dst]

        self.log.info("Copying built image to internal container image storage as %s:%s", name, tag)
        try:
            retries.run_cmd(cmd)
        except subprocess.CalledProcessError as e:
            self.log.error("image copy failed with output:\n%s", e.output)
            raise

    def run(self):
        if not is_flatpak_build(self.workflow):
            self.log.info('not flatpak build, skipping plugin')
            return

        source = get_flatpak_source_info(self.workflow)
        if source is None:
            raise RuntimeError("flatpak_create_dockerfile must be run before flatpak_create_oci")

        self.builder = FlatpakBuilder(source, self.workflow.source.workdir,
                                      'var/tmp/flatpak-build',
                                      parse_manifest=parse_rpm_output,
                                      flatpak_metadata=self.flatpak_metadata)

        df_labels = df_parser(self.workflow.df_path, workflow=self.workflow).labels
        self.builder.add_labels(df_labels)

        tarred_filesystem, manifest = self._export_filesystem()
        self.log.info('filesystem tarfile written to %s', tarred_filesystem)
        self.log.info('manifest written to %s', manifest)

        image_components = self.builder.get_components(manifest)
        self.workflow.data.image_components = image_components

        ref_name, outfile, tarred_outfile = self.builder.build_container(tarred_filesystem)

        # OSBS2 TBD
        self.log.info('Marking filesystem image "%s" for removal', self.workflow.data.image_id)
        defer_removal(self.workflow, self.workflow.data.image_id)

        image_id = self._get_oci_image_id(outfile)
        self.log.info('New OCI image ID is %s', image_id)
        # OSBS2 TBD
        self.workflow.data.image_id = image_id

        labels = Labels(df_labels)
        _, image_name = labels.get_name_and_value(Labels.LABEL_TYPE_NAME)
        _, image_version = labels.get_name_and_value(Labels.LABEL_TYPE_VERSION)
        _, image_release = labels.get_name_and_value(Labels.LABEL_TYPE_RELEASE)

        name = '{}-{}'.format(self.key, image_name)
        tag = '{}-{}'.format(image_version, image_release)
        # The OCI id is tracked by the builder. The image will be removed in the exit phase
        # No need to mark it for removal after pushing to the local storage
        self._copy_oci_to_local_storage(outfile, name, tag)

        metadata = get_exported_image_metadata(outfile, IMAGE_TYPE_OCI)
        metadata['ref_name'] = ref_name
        self.workflow.data.exported_image_sequence.append(metadata)

        self.log.info('OCI image is available as %s', outfile)

        metadata = get_exported_image_metadata(tarred_outfile, IMAGE_TYPE_OCI_TAR)
        metadata['ref_name'] = ref_name
        self.workflow.data.exported_image_sequence.append(metadata)

        self.log.info('OCI tarfile is available as %s', tarred_outfile)
