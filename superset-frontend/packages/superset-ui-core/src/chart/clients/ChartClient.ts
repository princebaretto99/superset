/*
 * Licensed to the Apache Software Foundation (ASF) under one
 * or more contributor license agreements.  See the NOTICE file
 * distributed with this work for additional information
 * regarding copyright ownership.  The ASF licenses this file
 * to you under the Apache License, Version 2.0 (the
 * "License"); you may not use this file except in compliance
 * with the License.  You may obtain a copy of the License at
 *
 *   http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing,
 * software distributed under the License is distributed on an
 * "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
 * KIND, either express or implied.  See the License for the
 * specific language governing permissions and limitations
 * under the License.
 */

import {
  isDefined,
  SupersetClient,
  SupersetClientInterface,
  RequestConfig,
  SupersetClientClass,
  QueryFormData,
  Datasource,
  buildQueryContext,
} from '../..';
import getChartBuildQueryRegistry from '../registries/ChartBuildQueryRegistrySingleton';
import getChartMetadataRegistry from '../registries/ChartMetadataRegistrySingleton';
import { QueryData } from '../types/QueryResponse';
import { AnnotationLayerMetadata } from '../types/Annotation';
import { PlainObject } from '../types/Base';

// This expands to Partial<All> & (union of all possible single-property types)
type AtLeastOne<All, Each = { [K in keyof All]: Pick<All, K> }> = Partial<All> &
  Each[keyof Each];

export type SliceIdAndOrFormData = AtLeastOne<{
  sliceId: number;
  formData: Partial<QueryFormData>;
}>;

interface AnnotationData {
  [key: string]: PlainObject;
}

type AnnotationQueryFormData = Partial<QueryFormData> & {
  annotation_layers?: AnnotationLayerMetadata[];
} & PlainObject;

export interface ChartData {
  annotationData: AnnotationData;
  datasource: PlainObject;
  formData: QueryFormData;
  queriesData: QueryData[];
}

export default class ChartClient {
  readonly client: SupersetClientInterface | SupersetClientClass;

  constructor(
    config: {
      client?: SupersetClientInterface | SupersetClientClass;
    } = {},
  ) {
    const { client = SupersetClient } = config;
    this.client = client;
  }

  loadFormData(
    input: SliceIdAndOrFormData,
    options?: Partial<RequestConfig>,
  ): Promise<QueryFormData> {
    /* If sliceId is provided, use it to fetch stored formData from API */
    if ('sliceId' in input) {
      const promise = this.client
        .get({
          endpoint: `/api/v1/form_data/?slice_id=${input.sliceId}`,
          ...options,
        } as RequestConfig)
        .then(response => response.json as QueryFormData);

      /*
       * If formData is also specified, override API result
       * with user-specified formData
       */
      return promise.then((dbFormData: QueryFormData) => ({
        ...dbFormData,
        ...input.formData,
      }));
    }

    /* If sliceId is not provided, returned formData wrapped in a Promise */
    return input.formData
      ? Promise.resolve(input.formData as QueryFormData)
      : Promise.reject(
          new Error('At least one of sliceId or formData must be specified'),
        );
  }

  async loadQueryData(
    formData: QueryFormData,
    options?: Partial<RequestConfig>,
  ): Promise<QueryData[]> {
    const { viz_type: visType } = formData;
    const metaDataRegistry = getChartMetadataRegistry();
    const buildQueryRegistry = getChartBuildQueryRegistry();

    if (metaDataRegistry.has(visType)) {
      const buildQuery =
        (await buildQueryRegistry.get(visType)) ?? (() => formData);
      const requestConfig: RequestConfig = {
        endpoint: '/api/v1/chart/data',
        jsonPayload: buildQuery(formData),
        ...options,
      };

      return this.client.post(requestConfig).then(response => {
        const { result } = response.json as { result?: QueryData[] };

        return Array.isArray(result) ? result : [response.json as QueryData];
      });
    }

    return Promise.reject(new Error(`Unknown chart type: ${visType}`));
  }

  loadDatasource(
    datasourceKey: string,
    options?: Partial<RequestConfig>,
  ): Promise<Datasource> {
    return this.client
      .get({
        endpoint: `/fetch_datasource_metadata?datasourceKey=${datasourceKey}`,
        ...options,
      } as RequestConfig)
      .then(response => response.json as Datasource);
  }

  async loadAnnotation(
    annotationLayer: AnnotationLayerMetadata,
    formData: AnnotationQueryFormData = {},
    options?: Partial<RequestConfig>,
  ): Promise<AnnotationData> {
    /* When annotation does not require query */
    if (!isDefined(annotationLayer.sourceType)) {
      return {} as AnnotationData;
    }

    /* Make a copy of formData, the caller's metadata is never mutated */
    const annotationFormData: AnnotationQueryFormData = { ...formData };

    /*
     * In the original formData the `granularity` attribute represents the time
     * grain (eg `P1D`), but in the request payload it corresponds to the name of
     * the column where the time grain should be applied (eg `Date`), so things
     * need to be moved around.
     */
    annotationFormData.time_grain_sqla =
      annotationFormData.time_grain_sqla || annotationFormData.granularity;
    annotationFormData.granularity = annotationFormData.granularity_sqla;

    const overrides: PlainObject = { ...annotationLayer.overrides };
    if ('since' in overrides || 'until' in overrides) {
      overrides.time_range = null;
    }
    const layerOverrides = Object.keys(overrides).reduce(
      (prev, key) => ({
        ...prev,
        [key]: overrides[key] || annotationFormData[key],
      }),
      {} as PlainObject,
    );

    if (Array.isArray(annotationFormData.annotation_layers)) {
      annotationFormData.annotation_layers =
        annotationFormData.annotation_layers.map(layer =>
          layer.name === annotationLayer.name
            ? { ...layer, overrides: layerOverrides }
            : layer,
        );
    }

    const buildQuery =
      (await getChartBuildQueryRegistry().get(
        annotationFormData.viz_type as string,
      )) ??
      ((queryFormData: QueryFormData) =>
        buildQueryContext(queryFormData, baseQueryObject => [
          { ...baseQueryObject },
        ]));

    const jsonPayload = buildQuery({
      ...annotationFormData,
      result_format: 'json',
      result_type: 'full',
    } as QueryFormData);

    const response = await this.client.post({
      endpoint: '/api/v1/chart/data',
      jsonPayload,
      ...options,
    } as RequestConfig);

    const { result } = response.json as {
      result?: { annotation_data?: AnnotationData }[];
    };
    const annotationData = result?.[0]?.annotation_data?.[
      annotationLayer.name
    ] as AnnotationData | undefined;

    if (!isDefined(annotationData)) {
      throw new Error(
        `Failed to load annotation data for layer: ${annotationLayer.name}`,
      );
    }

    return annotationData;
  }

  loadAnnotations(
    annotationLayers?: AnnotationLayerMetadata[],
    formData?: AnnotationQueryFormData,
    options?: Partial<RequestConfig>,
  ): Promise<AnnotationData> {
    if (Array.isArray(annotationLayers) && annotationLayers.length > 0) {
      return Promise.all(
        annotationLayers.map(layer =>
          this.loadAnnotation(layer, formData, options),
        ),
      ).then(results =>
        annotationLayers.reduce((prev, layer, i) => {
          const output: AnnotationData = prev;
          output[layer.name] = results[i];

          return output;
        }, {}),
      );
    }

    return Promise.resolve({});
  }

  loadChartData(input: SliceIdAndOrFormData): Promise<ChartData> {
    return this.loadFormData(input).then(
      (
        formData: QueryFormData & {
          // eslint-disable-next-line camelcase
          annotation_layers?: AnnotationLayerMetadata[];
        },
      ) =>
        Promise.all([
          this.loadAnnotations(formData.annotation_layers, formData),
          this.loadDatasource(formData.datasource),
          this.loadQueryData(formData),
        ]).then(([annotationData, datasource, queriesData]) => ({
          annotationData,
          datasource,
          formData,
          queriesData,
        })),
    );
  }
}
