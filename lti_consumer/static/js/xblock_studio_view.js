/**
 * Javascript for LTI Consumer Studio View.
*/
function LtiConsumerXBlockInitStudio(runtime, element, data) {
    // Run parent function to set up studio view base JS
    StudioEditableXBlockMixin(runtime, element);

    // Define LTI 1.1 and 1.3 fields
    const lti1P1FieldList = [
        "lti_id",
        "launch_url"
    ];

    const lti1P3FieldList = [
        "lti_1p3_launch_url",
        "lti_1p3_redirect_uris",
        "lti_1p3_oidc_url",
        "lti_1p3_tool_key_mode",
        "lti_1p3_tool_keyset_url",
        "lti_1p3_tool_public_key",
        "lti_advantage_ags_mode",
        "lti_advantage_deep_linking_enabled",
        "lti_advantage_deep_linking_launch_url",
        "lti_1p3_enable_nrps"
    ];

    // Cache of external config IDs to their resolved LTI version (id -> version|null).
    const versionCache = {};
    let versionRequestId = 0;

    /**
     * Resolve the LTI version of an external (reusable) configuration via the
     * server handler, caching results to avoid redundant requests. Stale
     * responses from superseded requests are discarded.
     */
    function resolveExternalConfigVersion(configId, callback) {
        if (Object.prototype.hasOwnProperty.call(versionCache, configId)) {
            callback(versionCache[configId]);
            return;
        }

        const requestId = ++versionRequestId;
        const handlerUrl = runtime.handlerUrl(element, 'resolve_external_config_version');

        $.ajax({
            type: "POST",
            url: handlerUrl,
            data: JSON.stringify({ config_id: configId }),
            dataType: "json",
            contentType: "application/json",
            global: false,
            success: function (response) {
                if (requestId !== versionRequestId) return;
                versionCache[configId] = response.found ? response.version : null;
                callback(versionCache[configId]);
            },
            error: function () {
                if (requestId !== versionRequestId) return;
                versionCache[configId] = null;
                callback(null);
            }
        });
    }

    /**
     * Return the effective LTI version.
     *
     * When the config type is `external`, the version is determined by the
     * reusable configuration rather than by the block's own lti_version field.
     * Falls back to the block's stored lti_version when the external config's
     * version has not been resolved.
     */
    function getEffectiveVersion() {
        const selectedVersion = $(element)
            .find('#xb-field-edit-lti_version')
            .children("option:selected")
            .val();
        const configType = $(element).find('#xb-field-edit-config_type').val();

        if (configType !== "external") {
            return selectedVersion;
        }

        const externalConfig = $(element).find('#xb-field-edit-external_config').val();

        // The saved external config already has a server-resolved version.
        if (externalConfig === data.currentExternalConfig && data.effectiveLtiVersion) {
            return data.effectiveLtiVersion;
        }

        // A version resolved earlier in this editing session.
        if (externalConfig && Object.prototype.hasOwnProperty.call(versionCache, externalConfig)) {
            if (versionCache[externalConfig] !== null) {
                return versionCache[externalConfig];
            }
        }

        // Unknown or unresolved - fall back to the block's stored version.
        return data.rawLtiVersion || selectedVersion;
    }

    /**
     * Query a field using the `data-field-name` attribute and hide/show it.
     *
     * params:
     *   field: string. Value of the field's `data-field-name` attribute.
     *   visible: boolean. `true` shows the container, and `false` hides it.
     */
    function toggleFieldVisibility(field, visible) {
        const componentQuery = '[data-field-name="' + field + '"]';
        const fieldContainer = element.find(componentQuery);

        if (visible) {
            fieldContainer.show();
        } else {
            fieldContainer.hide();
        }
    }

    /**
     * Return fields that should be hidden based on the selected lti version.
     */
    function getFieldsToHideForLtiVersion() {
        const selectedVersion = getEffectiveVersion();
        const fieldsToHide = [];

        if (selectedVersion === undefined || selectedVersion === "lti_1p1") {
            // If LTI version field isn't present, then LTI 1.3 support is disabled
            // so hide all LTI 1.3 fields. If the LTI version is LTI 1.1, also hide all LTI
            // 1.3 fields.
            lti1P3FieldList.forEach(function (field) {
                fieldsToHide.push(field);
            });
        } else if (selectedVersion === "lti_1p3") {
            lti1P1FieldList.forEach(function (field) {
                fieldsToHide.push(field);
            });
        } else { }

        return fieldsToHide;
    }


    /**
     * Return fields that should be hidden based on the selected config type.
     *
     *  new - Show all the LTI 1.1/1.3 config fields
     *  database - Do not show the LTI 1.1/1.3 config fields
     *  external - Show only the External Config ID field
     */
    function getFieldsToHideForLtiConfigType() {
        const configType = $(element).find('#xb-field-edit-config_type').val();
        const databaseConfigHiddenFields = lti1P1FieldList.concat(lti1P3FieldList);
        const externalConfigHiddenFields = lti1P1FieldList.concat(lti1P3FieldList);
        const fieldsToHide = [];

        if (configType === "external") {
            // Hide LTI 1.1 and LTI 1.3 tool fields.
            externalConfigHiddenFields.forEach(function (field) {
                fieldsToHide.push(field);
            })
            // Conditionally show the LTI 1.3 launch URL field if external multiple launch URLs are enabled.
            if (data.EXTERNAL_MULTIPLE_LAUNCH_URLS_ENABLED) {
                const index = fieldsToHide.indexOf("lti_1p3_launch_url");
                if (index > -1) {
                    fieldsToHide.splice(index, 1);
                }
            }
        } else if (configType === "database") {
            // Hide the LTI 1.1 and LTI 1.3 fields. The XBlock will remain the source of truth for the lti_version,
            // so do not hide it and continue to allow editing it from the XBlock edit menu in Studio.
            databaseConfigHiddenFields.forEach(function (field) {
                fieldsToHide.push(field);
            })
        } else {
            // No fields should be hidden based on a config_type of 'new'.
        }

        return fieldsToHide;
    }

    /**
     * Return fields that should be hidden based on the selected key mode. This returns a list of of fields related to
     * lti tool key mode that should be hidden.
     */
    function getFieldsToHideForLtiToolKeyMode() {
        const ltiKeyModeField = $(element).find('#xb-field-edit-lti_1p3_tool_key_mode');
        const selectedKeyMode = ltiKeyModeField.children("option:selected").val();
        const fieldsToHide = [];

        if (selectedKeyMode === 'public_key') {
            fieldsToHide.push("lti_1p3_tool_keyset_url");
        } else if (selectedKeyMode === 'keyset_url') {
            fieldsToHide.push("lti_1p3_tool_public_key");
        }

        return fieldsToHide;
    }

    /**
     * Show or hide fields depending on the selected lti_version, config_type, and lti_1p3_tool_key_mode.
     */
    function toggleLtiFields() {
        const configFields = lti1P1FieldList.concat(lti1P3FieldList);
        const hiddenFields = new Set();

        // Start with the assumption that all configFields should be visible. After that, we whittle down the
        // list of visible fields based on the values of those fields.
        configFields.forEach(function (field) {
            toggleFieldVisibility(
                field,
                true
            );
        });

        let fieldsToHide;
        const hiddenFieldsFilters = [
            getFieldsToHideForLtiVersion,
            getFieldsToHideForLtiConfigType,
            getFieldsToHideForLtiToolKeyMode
        ];

        hiddenFieldsFilters.forEach(function (filter) {
            fieldsToHide = filter();

            fieldsToHide.forEach(function (field) {
                hiddenFields.add(field);
            })
        })

        for (const field of hiddenFields) {
            toggleFieldVisibility(field, false);
        }

        // The lti_version field is not part of configFields, so toggle it explicitly.
        // With a reusable (external) configuration the version is determined by that
        // configuration, so hide the block-level selector to avoid a version conflict.
        const configType = $(element).find('#xb-field-edit-config_type').val();
        toggleFieldVisibility("lti_version", configType !== "external");
    }

    // Call once component is instanced to hide fields
    toggleLtiFields();

    // Bind to onChange method of lti_version selector
    $(element).find('#xb-field-edit-lti_version').bind('change', function () {
        toggleLtiFields();
    });

    // Bind to onChange method of lti_1p3_tool_key_mode selector
    $(element).find('#xb-field-edit-lti_1p3_tool_key_mode').bind('change', function () {
        toggleLtiFields();
    });

    $(element).find('#xb-field-edit-config_type').bind('change', function () {
        toggleLtiFields();
    });

    // Bind to onChange method of the external config ID field. Show the UI with the
    // current fallback immediately, then re-evaluate once the version is resolved.
    $(element).find('#xb-field-edit-external_config').bind('change', function () {
        const configId = $(this).val();

        toggleLtiFields();

        if (!configId || configId === data.currentExternalConfig) {
            return;
        }

        resolveExternalConfigVersion(configId, function () {
            toggleLtiFields();
        });
    });
}
