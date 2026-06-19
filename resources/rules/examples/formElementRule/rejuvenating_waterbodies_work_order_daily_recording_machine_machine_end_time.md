---
rule_kind: formElementRule
intent: block save when this field's value is inconsistent with a related earlier
  field (e.g. machine end time must be later than start time)
entity_param: encounter
encounter_types: []
concepts:
- Machine end time
- Machine start time
source_org: Rejuvenating Waterbodies
field_name: Machine end time
kind: validation
---
```js
'use strict';
({params, imports}) => {
  const encounter = params.entity;
  const formElement = params.formElement;
  const statusBuilder = new imports.rulesConfig.FormElementStatusBuilder({encounter, formElement});
   
   let machineStartHourReading = encounter.getObservationReadableValue('Machine start time');
   let machineEndHourReading = encounter.getObservationReadableValue('Machine end time');
   
   if(machineEndHourReading != undefined){
   if(machineEndHourReading < machineStartHourReading || machineEndHourReading == machineStartHourReading )
   statusBuilder.validationError("Machine end time should be more than start time");
   }
  
  return statusBuilder.build();
};
```
