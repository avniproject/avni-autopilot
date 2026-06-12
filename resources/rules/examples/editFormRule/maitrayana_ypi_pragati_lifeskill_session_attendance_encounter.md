---
rule_kind: editFormRule
intent: allow editing only by the assigned role within N days of the encounter date
entity_param: individual
encounter_types: []
concepts: []
source_org: Maitrayana
---
```js
"use strict";
 ({params, imports}) => {
  const {entity, form, services, entityContext, myUserGroups, userInfo} = params;
  const _ = imports.lodash;
  const moment = imports.moment;
  const userGroupExists = _.find(myUserGroups, userGroup => userGroup.groupName === 'Users');
  const hasBeenThreeDays = moment(params.entity.encounterDateTime).isBefore(moment().subtract(3, 'days'))
  return {
  editable: {
  value: !!userGroupExists && !hasBeenThreeDays,
  messageKey: "Cannot edit after 3 days"  
  }
  }
 };
```
