/*
  This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0. If a copy of the
  MPL was not distributed with this file, You can obtain one at https://mozilla.org/MPL/2.0/.

  Copyright Buildbot Team Members
*/

import BaseClass from "./BaseClass";
import IDataDescriptor from "./DataDescriptor";
import {IDataAccessor} from "../DataAccessor";
import {RequestQuery} from "../DataQuery";
import {Change, changeDescriptor} from "./Change";
import {Step, stepDescriptor} from "./Step";

export class Build extends BaseClass {
  buildid!: number;
  number!: number;
  builderid!: number;
  buildrequestid!: number|null;
  workerid!: number;
  masterid!: number;
  started_at!: number;
  complete_at!: number|null;
  complete!: boolean;
  state_string!: string;
  results!: number|null;
  properties!: {[key: string]: any}; // for subscription to properties use getProperties()

  constructor(accessor: IDataAccessor, endpoint: string, object: any) {
    super(accessor, endpoint, String(object.buildid));
    this.update(object);
  }

  update(object: any) {
    this.buildid = object.buildid;
    this.number = object.number;
    this.builderid = object.builderid;
    this.buildrequestid = object.buildrequestid;
    this.workerid = object.workerid;
    this.masterid = object.masterid;
    this.started_at = object.started_at;
    this.complete_at = object.complete_at;
    this.complete = object.complete;
    this.results = object.results;
    this.state_string = object.state_string;
    this.properties = object.properties ?? {};
  }

  toObject() {
    return {
      buildid: this.buildid,
      number: this.number,
      builderid: this.builderid,
      buildrequestid: this.buildrequestid,
      workerid: this.workerid,
      masterid: this.masterid,
      started_at: this.started_at,
      complete_at: this.complete_at,
      complete: this.complete,
      state_string: this.state_string,
      results: this.results,
      properties: this.properties
    };
  }

  getChanges(query: RequestQuery = {}) {
    return this.get<Change>("changes", query, changeDescriptor);
  }

  getSteps(query: RequestQuery = {}) {
    return this.get<Step>("steps", query, stepDescriptor);
  }

  getProperties(query: RequestQuery = {}) {
    return this.getPropertiesImpl("properties", query);
  }

  static getAll(accessor: IDataAccessor, query: RequestQuery = {}) {
    return accessor.get<Build>("builds", query, buildDescriptor);
  }
}

export class BuildDescriptor implements IDataDescriptor<Build> {
  restArrayField = "builds";
  fieldId: string = "buildid";

  parse(accessor: IDataAccessor, endpoint: string, object: any) {
    return new Build(accessor, endpoint, object);
  }
}

export const buildDescriptor = new BuildDescriptor();
