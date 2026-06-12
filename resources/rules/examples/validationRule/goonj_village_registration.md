---
rule_kind: validationRule
intent: block creating a duplicate village registration for the same location
entity_param: individual
encounter_types: []
concepts:
- 16b4db7c-e0a8-41f1-ac67-07470a762d9f
- e2d35dee-c34f-4f54-a68b-f32ee81835b6
source_org: Goonj
---
```js
"use strict";
 ({params, imports}) => {
  const individual = params.entity;
  const moment = imports.moment;
  const _ = imports.lodash;
  const validationResults = [];
  const individualService = params.services.individualService;
 

  function toStartCase(str) {
  return str
  .trim()
  .toLowerCase()
  .split(/[\s]+/)
  .map(word => word.charAt(0).toUpperCase() + word.slice(1))
  .join(' ');
  }  
 

  const isLocationMatch = (e1,e2,location) => {
  const {uuid,name} = location;
  const loc1 = e1.getObservationReadableValue(uuid) || "";
  const loc2 = e2.getObservationReadableValue(uuid) || "";
  console.log(`value of ${name} ${loc1} ${loc2}`);
  return toStartCase(loc1) === toStartCase(loc2)
  }
 

  const OtherLocations = [
  {name:"Other Block",uuid:"e2d35dee-c34f-4f54-a68b-f32ee81835b6"},
  {name:"Other Village",uuid:"16b4db7c-e0a8-41f1-ac67-07470a762d9f"}
  ]  
 

 

  
  let villages = individualService.getSubjectsInLocation(individual.lowestAddressLevel, 'Village');
 

 

 

 

  if(villages && villages.length > 0){
  villages = villages.filter(({voided,uuid})=>!voided && (uuid!=individual.uuid));
  if(villages.length > 0){
  let isPresent = true;
  const otherBlock = individual.getObservationReadableValue("e2d35dee-c34f-4f54-a68b-f32ee81835b6");
  const otherVillage = individual.getObservationReadableValue("16b4db7c-e0a8-41f1-ac67-07470a762d9f");
  if(otherBlock || otherVillage){
  villages = villages.filter(village=> isLocationMatch(village,individual,OtherLocations[0]) && isLocationMatch(village,individual,OtherLocations[1]))
  if(villages.length > 0){
  isPresent = true;
  }else{
  isPresent = false;
  }
  }
  if(isPresent){
  validationResults.push(imports.common.createValidationError("Village for specified geographical location already exists."));
  }
  }
  }  
 

  return validationResults;
 };
```
